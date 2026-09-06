This is a confirmed, valid finding. Let me lay out the evidence.

**The broken equality:** The code assumes "a POST to `/shutdown` received on this socket" implies "issued by the trusted, co-located node process" — but the dispatch performs zero verification of that assumption.

**The code:** [1](#0-0) 

In `SignerEventReceiver::next_event`, the request router checks `request.url()` values in sequence (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, `/new_block`) with no authentication layer at all — confirmed by the absence of any `auth_token`/`Authorization` check anywhere in `libsigner/**` [2](#0-1) . When `/shutdown` matches, it unconditionally stores `true` into `stop_signal` (an `Arc<AtomicBool>` shared with `main_loop`) and returns `Err(EventError::Terminated)` — no body validation, no secret, no peer-address check.

This is the exact same wire format the legitimate `SignerStopSignaler::send()` uses internally to self-terminate the receiver from the runloop thread [3](#0-2) , meaning the "authenticated" shutdown path and an attacker-forged one are byte-for-byte indistinguishable at the handler.

**Effect on `main_loop`:** once `stop_signal` is `true`, `is_stopped()` returns `true` forever [4](#0-3) , and the trait's `main_loop` breaks out permanently on the next iteration [5](#0-4) . There is no way to un-set this flag — the event-receiver thread exits and joins, per `RunningSigner`/`Signer::spawn` wiring in `libsigner/src/runloop.rs` [6](#0-5) .

**Reachability:** the auth_token mechanism found elsewhere in the codebase (`postblock_proposal.rs`, `stackslib/src/net/httpcore.rs`) protects the *node's* RPC endpoint, not the signer's inbound event-listener socket — the signer's HTTP server has no equivalent gate. The documented/sample signer binary configuration explicitly recommends binding this listener to all interfaces (`endpoint = "0.0.0.0:30000"`) rather than loopback-only [7](#0-6) , so this socket is not inherently loopback-restricted by the codebase — reachability depends on operator firewalling, which the code does nothing to enforce.

### Title
Unauthenticated `POST /shutdown` permanently kills the signer's event receiver - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` dispatches on `request.url() == "/shutdown"` and unconditionally sets `stop_signal` to `true`, terminating the receiver's `main_loop` with no authentication, secret, or origin check. Any remote party able to reach the signer's event-listener socket can send this single crafted HTTP request to permanently stop the signer from processing all future StackerDB chunks, block proposals, and burn-block events.

### Finding Description
The equality the code implicitly relies on — "a `/shutdown` POST received on this socket == a shutdown command issued by the co-located, trusted stacks-node process" — is never checked. The handler branch at [1](#0-0)  stores `true` into the shared `Arc<AtomicBool>` `stop_signal` and returns `Err(EventError::Terminated)` regardless of who sent the request or what the body contains. This is the identical wire protocol used internally by `SignerStopSignaler::send()` [8](#0-7) , so a forged request is indistinguishable from a legitimate one. Once set, `is_stopped()` never returns to `false` [4](#0-3) , so `EventReceiver::main_loop` breaks on its next iteration and exits for good [5](#0-4) . No other endpoint in this dispatcher (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) performs any credential check either, confirming the entire HTTP surface of this receiver is unauthenticated.

### Impact Explanation
A single unauthenticated HTTP POST permanently halts the signer's event stream: it will no longer receive StackerDB chunk events, block-validation responses, burn-block events, or new-block events forwarded from the co-located stacks-node. Since the signer thread cannot resume (the stop flag is never cleared and the thread has exited), this is effectively a permanent denial of service against the signer process requiring a full restart to recover — matching the "Critical - remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
The only precondition is network reachability to the signer's bound event-listener address/port. The codebase's own documentation/sample config for the signer binary recommends binding this listener on `0.0.0.0` [7](#0-6)  rather than loopback, and nothing in `libsigner` enforces a loopback-only bind or peer-address filtering at `bind()` [9](#0-8) . Attacker cost is a single crafted TCP connection and HTTP request with no credentials; the attack is a one-shot, not requiring repetition or any special peer/config state.

### Recommendation
Require a shared secret (e.g., the same `auth_token`/`auth_password` already used to coordinate node↔signer communication) to be validated on the `/shutdown` route (and ideally all routes) before acting on it, and/or restrict the listener bind and accept-loop to verified local/loopback peers.

### Proof of Concept
Rust test in `libsigner`:
1. Construct a `SignerEventReceiver<SignerMessage>` and call `bind()` on `127.0.0.1:0`, spawn `main_loop()` in a thread.
2. From a separate `TcpStream::connect` to the bound address (simulating an unauthenticated remote peer, no token/header), send `"POST /shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"`.
3. Assert `event_receiver.is_stopped()` becomes `true` and the spawned `main_loop` thread joins/exits shortly after.
4. Assert that a legitimate subsequent `POST /stackerdb_chunks` with valid payload sent to the same address is never processed (connection refused/loop already exited), demonstrating permanent loss of service — this mirrors exactly the existing `SignerStopSignaler::send()` code path at [8](#0-7)  but invoked by an unauthenticated third party instead of the legitimate runloop.

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

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
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

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```

**File:** libsigner/src/runloop.rs (L228-247)
```rust
        // start a thread for the event receiver
        let event_thread = thread::Builder::new()
            .name(format!("event_receiver:{bind_port}"))
            .stack_size(THREAD_STACK_SIZE)
            .spawn(move || event_receiver.main_loop())
            .map_err(|e| {
                error!("EventReceiver failed to start: {:?}", &e);
                EventError::FailedToStart
            })?;

        // start receiving events and doing stuff with them
        let runloop_thread = thread::Builder::new()
            .name(format!("signer_runloop:{bind_port}"))
            .stack_size(THREAD_STACK_SIZE)
            .spawn(move || signer_loop.main_loop(event_recv, result_sender, stop_signaler))
            .map_err(|e| {
                error!("SignerRunLoop failed to start: {:?}", &e);
                ret_stop_signaler.send();
                EventError::FailedToStart
            })?;
```

**File:** docs/signing.md (L42-49)
```markdown
```toml
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```
```
