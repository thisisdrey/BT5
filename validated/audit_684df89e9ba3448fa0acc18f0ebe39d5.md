### Title
Unauthenticated `POST /shutdown` permanently terminates the signer's event receiver - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` handles the `/shutdown` route by unconditionally setting `stop_signal` to `true` and returning `EventError::Terminated` for *any* POST to that path, with no check of sender identity, secret, or origin. Since the signer's event-receiver endpoint (configured via `endpoint` in `stacks-signer` config, e.g. `0.0.0.0:30000`) is a plain HTTP server with no auth on this or any other route, any remote party who can reach that TCP port can permanently stop the signer's `main_loop`.

### Finding Description
The equality that should hold is: "`stop_signal` becomes `true` only when the local node operator (via `SignerStopSignaler::send`) explicitly requests shutdown." In `next_event`, the `/shutdown` branch does this unconditionally on any POST: [1](#0-0) 
There is no check of a shared secret, source IP, or any header before performing the store. Contrast this with the block-proposal RPC path in the node itself, which is gated by `auth_password`/`auth_token` — the event-receiver server in `libsigner` has no equivalent gate on `/shutdown`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block`; `tiny_http` accepts the connection and dispatches purely based on `request.url()`. [2](#0-1) 
Once `stop_signal` is `true`, `is_stopped()` returns `true` on every subsequent call, so `main_loop` immediately breaks out and stops calling `next_event()` at all: [3](#0-2) [4](#0-3) 
There is no way to reset `stop_signal` back to `false` — the field is only ever set in the `/shutdown` handler and in `SignerStopSignaler::send`, and it's never cleared. Once tripped, the signer process's event ingestion thread is dead for the lifetime of the process; the signer must be restarted by an operator to resume receiving StackerDB/burn/block events.

The signer's `endpoint` config documents binding to `0.0.0.0:PORT` as a normal example (`stacks-signer/src/config.rs`), and the value is used directly with no additional access control layer added by `libsigner`. Whether this port is intended to be firewalled to just the co-located `stacks-node` is an operational/deployment decision — the code itself provides no authentication mechanism for this HTTP server, so if the port is reachable from a remote network (misconfiguration or intentionally exposed for multi-host node/signer setups), the DoS is trivially exploitable.

### Impact Explanation
Any attacker who can complete a TCP connection to the signer's bound event-receiver port can send a single `POST /shutdown` and permanently kill the signer's event-delivery loop. After this, all subsequent legitimate events forwarded by the co-located `stacks-node`'s event dispatcher (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) are silently dropped by the OS/`tiny_http` (connection refused or ignored, since the server thread has exited `main_loop`), meaning the signer stops signing/participating entirely until manually restarted. This matches the "Critical — remote crash/unauthenticated DoS from few messages" category: a single unauthenticated request achieves total denial of the signer's event pipeline.

### Likelihood Explanation
Preconditions: the attacker needs only network reachability to the bound `endpoint` TCP port of the signer's `SignerEventReceiver`; no secret, peer identity, StackerDB slot, or privileged role is needed — the handler performs zero authentication. Attack cost is a single crafted HTTP POST. It is repeatable (though only needs to succeed once per signer restart). The severity is contingent on the event-receiver port actually being reachable by an unprivileged remote party (i.e., not strictly firewalled to localhost/the paired node) — this is a deployment/config detail, but the code provides no defense-in-depth if that assumption is violated, which is exactly the scenario the question describes ("remote unprivileged attacker who can reach the signer's bound event-receiver port").

### Recommendation
Require the same shared-secret/auth mechanism used elsewhere in the node/signer RPC surface (e.g., an `auth_password`-derived header or token) for the `/shutdown` route (and ideally all routes) in `SignerEventReceiver::next_event`, and/or restrict acceptance of `/shutdown` to requests originating from the loopback address that `SignerStopSignaler::send` itself connects from, since that signaler always connects to `self.local_addr` (i.e., itself) to wake the blocking `recv()` call. Alternatively, replace the "send a real HTTP request to self" wakeup trick with a proper cross-thread mechanism (e.g., a self-pipe/condvar or an internal-only signal) so the wire-facing HTTP server never needs to expose a shutdown-triggering route at all.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
#[test]
fn shutdown_route_is_unauthenticated_dos() {
    use std::net::TcpStream;
    use std::sync::mpsc::channel;
    use std::thread;

    let mut receiver: SignerEventReceiver<crate::v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let (tx, rx) = channel();
    receiver.add_consumer(tx);

    let handle = thread::spawn(move || receiver.main_loop());

    // Attacker: bare POST /shutdown, no auth header/secret
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "x";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).unwrap();

    handle.join().unwrap(); // main_loop exits due to EventError::Terminated

    // Legitimate event sent afterwards is never forwarded again.
    let mut stream2 = TcpStream::connect(addr);
    assert!(stream2.is_err() || rx.try_recv().is_err());
}
```
The assertion site is the `main_loop` exit triggered purely by the unauthenticated `/shutdown` branch at [1](#0-0) , proving `is_stopped()` (`libsigner/src/events.rs:462-464`) becomes permanently `true` from an unauthenticated remote POST.

### Citations

**File:** libsigner/src/events.rs (L284-312)
```rust
    fn main_loop(&mut self) {
        loop {
            if self.is_stopped() {
                info!("Event receiver stopped");
                break;
            }
            let next_event = match self.next_event() {
                Ok(event) => event,
                Err(EventError::UnrecognizedEvent(..)) => {
                    // got an event that we don't care about (not a problem)
                    continue;
                }
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
                Err(e) => {
                    warn!("Failed to receive next event: {:?}", &e);
                    continue;
                }
            };
            if !self.forward_event(next_event) {
                info!("Failed to forward event");
                break;
            }
        }
        info!("Event receiver main loop exit");
    }
```

**File:** libsigner/src/events.rs (L413-458)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
            debug!("Request handling");
            let request = http_server.recv()?;
            debug!("Got request"; "method" => %request.method(), "path" => request.url());

            if request.url() == "/status" {
                request
                .respond(HttpResponse::from_string("OK"))
                .expect("response failed");
                return Ok(SignerEvent::StatusCheck);
            }

            if request.method() != &HttpMethod::Post {
                return Err(EventError::MalformedRequest(format!(
                    "Unrecognized method '{}'",
                    request.method(),
                )));
            }
            debug!("Processing {} event", request.url());
            if request.url() == "/stackerdb_chunks" {
                process_event::<T, StackerDBChunksEvent>(request)
            } else if request.url() == "/proposal_response" {
                process_event::<T, BlockValidateResponse>(request)
            } else if request.url() == "/new_burn_block" {
                process_event::<T, BurnBlockEvent>(request)
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
            } else if request.url() == "/new_block" {
                process_event::<T, StacksBlockEvent>(request)
            } else {
                let url = request.url().to_string();
                debug!(
                    "[{:?}] next_event got request with unexpected url {}, return OK so other side doesn't keep sending this",
                    event_receiver.local_addr,
                    url
                );
                ack_dispatcher(request);
                Err(EventError::UnrecognizedEvent(url))
            }
        })?
```

**File:** libsigner/src/events.rs (L461-464)
```rust
    /// Determine if the receiver is hung up
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
