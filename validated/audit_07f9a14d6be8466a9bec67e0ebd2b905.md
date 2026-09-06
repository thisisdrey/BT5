### Title
Unauthenticated `POST /shutdown` on signer event-receiver socket causes remote DoS - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver::next_event` HTTP dispatch treats `/shutdown` as a plain, unauthenticated route: any TCP client that can reach the bound event-receiver socket can send a bare `POST /shutdown` request and immediately flip the receiver's stop flag. This breaks the intended invariant that only the node operator's `SignerStopSignaler::send()` can terminate the signer's event loop.

### Finding Description
The intended equality is: *the only party able to terminate the signer's event-processing loop is the local node operator, via `SignerStopSignaler::send()`*. In `libsigner/src/events.rs`, `next_event` dispatches on `request.url()` with no credential, origin, or source-address check anywhere in the function: [1](#0-0) 

The `/shutdown` branch performs `event_receiver.stop_signal.store(true, Ordering::SeqCst); Err(EventError::Terminated)` unconditionally for any request whose URL matches, regardless of who sent it. Compare this to `SignerStopSignaler::send()`, whose only "security" is that it connects to `self.local_addr` and issues the exact same unauthenticated `POST /shutdown` request: [2](#0-1) 

There is no shared secret, token, or peer/IP allow-list checked before honoring `/shutdown` — the "authorization" is entirely by construction (only the operator's process is expected to connect), not by verification. Once `Err(EventError::Terminated)` propagates out of `next_event()`, `main_loop`'s match arm breaks the loop: [3](#0-2) 

Any TCP client — not just the local operator process — that can open a connection to the bound `HttpServer` (`SignerEventReceiver::bind`, `libsigner/src/events.rs:404-408`) can send this raw HTTP request and terminate the receiver, since `tiny_http`'s `Request` object carries no origin/auth information that is checked here.

### Impact Explanation
A single unauthenticated `POST /shutdown HTTP/1.1` request permanently halts the signer's `main_loop`, stopping it from processing further `stackerdb_chunks`, `proposal_response`, `new_burn_block`, and `new_block` events from the node. This is a full denial-of-service of the signer's event pipeline, repeatable at will (the attacker can keep re-sending it if the process is restarted or the socket rebinds), matching the "Critical - remote crash/unauthenticated DoS from a few messages" category. This affects whichever party can reach the bound TCP port (`libsigner::SignerEventReceiver`'s configured `endpoint`).

### Likelihood Explanation
Exploitability depends entirely on network reachability of the event-receiver bind address. If the signer operator binds it to `127.0.0.1` (loopback) as is the common deployment pattern, this is not remotely reachable by an unprivileged network attacker and the finding would not apply. The question's premise — "bind() listens on all configured interfaces" — asserts the bind address is attacker-reachable (e.g., `0.0.0.0` or a non-loopback interface), which is a deployment/configuration choice, not something enforced or defended against in `libsigner/src/events.rs` itself. Given that premise, the attack requires no credentials, no state, and a single crafted request, so likelihood is high whenever the operator's config exposes the socket beyond loopback.

### Recommendation
Add authentication/authorization to the `/shutdown` route (and ideally all routes) in `SignerEventReceiver::next_event` — e.g., require a shared secret/token configured out-of-band between the event dispatcher and the signer, or restrict `/shutdown` handling to loopback-originated connections by checking the peer address available from the underlying `tiny_http::Request`/socket. At minimum, document and default-enforce that the event-receiver endpoint must bind to loopback only, and reject bind requests to non-loopback addresses unless an explicit auth mechanism is configured.

### Proof of Concept
```rust
// In libsigner::events test module
#[test]
fn test_unauthenticated_shutdown_terminates_receiver() {
    let mut receiver: SignerEventReceiver<crate::v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
    // bind() requires a fixed port in real code; use a known free port for the test
    let bound = receiver.bind("127.0.0.1:39999".parse().unwrap()).unwrap();

    // Attacker: plain TcpStream, no credentials, no prior interaction
    let mut stream = TcpStream::connect(bound).unwrap();
    let body = "attacker";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        bound, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // next_event() should return Err(EventError::Terminated) and set is_stopped()
    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped());
}
```
This reproduces the crash/termination site at `libsigner/src/events.rs:443-445`, where the `/shutdown` branch is reached with zero authentication check, and confirms the resulting `EventError::Terminated` would break `main_loop` per `libsigner/src/events.rs:296-300`.

### Citations

**File:** libsigner/src/events.rs (L290-300)
```rust
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

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```
