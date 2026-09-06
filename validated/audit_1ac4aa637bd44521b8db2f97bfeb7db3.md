### Title
Unauthenticated `POST /shutdown` permanently terminates the signer's event receiver - ([File: libsigner/src/events.rs])

### Summary
The `SignerEventReceiver::next_event` HTTP dispatch matches `request.url() == "/shutdown"` and unconditionally sets `stop_signal` to `true` and returns `Err(EventError::Terminated)`, with no credential, secret, or origin check on this branch. Any TCP client that can reach the signer's event-listener socket can send this request and permanently halt the signer's event-processing loop.

### Finding Description
`SignerEventReceiver::next_event` dispatches based solely on `request.url()` and `request.method()`, with branches for `/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, and `/new_block` [1](#0-0) . The `/shutdown` branch performs `event_receiver.stop_signal.store(true, Ordering::SeqCst); Err(EventError::Terminated)` with no verification that the request originated from the co-located, trusted node process (e.g. no shared secret, no localhost-only enforcement, no token comparison) [2](#0-1) . The legitimate stop mechanism, `SignerStopSignaler::send`, itself works by opening a plain `TcpStream` to `local_addr` and issuing exactly this `POST /shutdown` request with no credential, confirming that the protocol itself carries no authentication and any party able to open a TCP connection to that socket can replicate it byte-for-byte [3](#0-2) . Once `stop_signal` is `true`, `is_stopped()` returns `true` permanently (no reset path exists) [4](#0-3) , causing `main_loop`'s `next_event()` call to first hit the early-return `Err(EventError::Terminated)` check in `next_event` [5](#0-4) , which `main_loop` treats as "we're done" and `break`s out for good [6](#0-5) .

### Impact Explanation
Any remote party that can reach the signer's event-listener socket and send a single `POST /shutdown HTTP/1.1` request permanently disables that signer's event stream: it stops receiving StackerDB chunks, block-validation responses, burn-block/new-block events, and status checks from the node — for the lifetime of the process, with no automatic recovery, since `stop_signal` is a one-way flag. This is a Critical unauthenticated DoS: one crafted message with zero credentials permanently silences the signer runloop's inputs, matching "remote crash/unauthenticated DoS from few messages."

### Likelihood Explanation
Preconditions: the attacker only needs TCP reachability to the address/port `SignerEventReceiver::bind` listens on (the event-observer endpoint the node is configured to push events to). No secret, peer identity, StackerDB slot, or privileged role is required — the code path is reached before any authentication check and the "legitimate" shutdown path (`SignerStopSignaler::send`) itself uses no secret, proving the wire protocol is inherently unauthenticated. Cost is a single short-lived TCP connection and one HTTP request; the effect is permanent and immediately repeatable against any signer whose listener is reachable (e.g. bound to a non-loopback interface or reachable through port-forwarding/misconfiguration).

### Recommendation
Require an authentication check on the `/shutdown` (and ideally all event-ingestion) routes before flipping `stop_signal` — e.g., verify a shared secret/token configured out-of-band between the node and the signer, or restrict `bind` to loopback-only and enforce that the request's peer address is loopback before honoring `/shutdown`. At minimum, do not allow an unauthenticated network peer to terminate the receiver; the shutdown signal should instead be delivered via an in-process channel/mechanism rather than a public HTTP endpoint with no credential.

### Proof of Concept
Rust test in `libsigner/src/events.rs` or a new integration test module:
1. Construct `SignerEventReceiver::<T>::new(false)`, call `bind("127.0.0.1:0".parse().unwrap())` to get the bound `SocketAddr`.
2. Spawn a thread running `main_loop()` (or repeatedly call `next_event()`).
3. From a separate `TcpStream::connect(addr)`, write `"POST /shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"` — no headers proving any secret/identity.
4. Assert `event_receiver.is_stopped() == true` after the request is processed.
5. Assert subsequent calls to `next_event()` return `Err(EventError::Terminated)` indefinitely, and that `main_loop()` has exited (`break` reached at [6](#0-5) ), verifying the event stream is permanently down with no legitimate credential ever presented.

### Citations

**File:** libsigner/src/events.rs (L296-300)
```rust
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

**File:** libsigner/src/events.rs (L416-418)
```rust
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
```

**File:** libsigner/src/events.rs (L423-447)
```rust
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
```

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
