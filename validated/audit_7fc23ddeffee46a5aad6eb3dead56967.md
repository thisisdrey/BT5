### Title
Unauthenticated `/shutdown` HTTP request permanently terminates the signer's event-receiving loop - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` dispatches on request URL alone; any POST to `/shutdown` unconditionally sets `stop_signal` and returns `EventError::Terminated`, with no check of sender identity, origin, or a shared secret. Since `main_loop` treats `EventError::Terminated` as a clean exit signal, one unauthenticated request permanently kills the signer's event-processing thread.

### Finding Description
The broken equality is: the code assumes "party that can open a TCP connection and send `POST /shutdown`" == "the local node process that legitimately wants to shut this receiver down," but nothing enforces that equality. In `next_event`, the dispatch table is purely URL-based: [1](#0-0) 

There is no comparison against `SignerStopSignaler::send`'s exact body/headers, no shared secret, no loopback-only check, and no TLS/auth of any kind — merely `request.url() == "/shutdown"` triggers `event_receiver.stop_signal.store(true, Ordering::SeqCst)` followed by `Err(EventError::Terminated)`. `main_loop` treats `Terminated` as a deliberate, successful stop and `break`s out of the loop: [2](#0-1) 

`SignerStopSignaler::send` (the legitimate caller) merely crafts a plain-text raw HTTP request over a fresh `TcpStream::connect`, with no signing or secret embedded: [3](#0-2) 

Any TCP client that can reach the bound listener can replicate this exact request (or even a minimal `POST /shutdown HTTP/1.1\r\n\r\n`) and achieve the identical effect, since the handler never inspects the body, headers, or peer address.

### Impact Explanation
A single unauthenticated HTTP POST to `/shutdown` permanently disables the signer's event-receiving main loop (`EventReceiver::main_loop`), which is how the signer receives `BlockProposal`, `StackerDBChunksEvent`, `BurnBlockEvent`, and `BlockValidateResponse` notifications from its co-located node. Once stopped, the signer stops consenting/observing consensus-critical events until manually restarted, i.e., a Critical unauthenticated remote crash/DoS of a single component from one message. This matches the "Critical - remote crash/unauthenticated DoS from few messages" category. The library itself documents the exposure risk of this endpoint when reachable beyond localhost, in the warning emitted in `SpawnedSigner::new`: [4](#0-3) , confirming the bind address (`config.endpoint`) is operator-configurable and not hardcoded to loopback.

### Likelihood Explanation
The only precondition is network reachability to the configured `endpoint` socket that `SignerEventReceiver::bind` listens on (`stacks-signer/src/lib.rs` `SpawnedSigner::new`, passing `config.endpoint` to `signer.spawn`). No secret, peer key, StackerDB slot ownership, or any privileged role is required — exactly the "unprivileged remote attacker who can connect to a reachable port" threat model in scope. The attack is trivially repeatable (each reconnect+POST re-arms after a restart) and costs a single TCP connection and a short HTTP request.

### Recommendation
Require an authenticated/local-only shutdown mechanism: bind the control channel to loopback only, or require a random per-process secret/token embedded by `SignerStopSignaler::send` and verified in `next_event` before honoring `/shutdown`, rejecting any request lacking a valid token instead of unconditionally trusting the URL path.

### Proof of Concept
```rust
// libsigner test: connect raw TCP to the bound SignerEventReceiver and send a bare
// "POST /shutdown" request with no auth, then assert the loop stops.
let mut receiver = SignerEventReceiver::<SomeMessageType>::new(false);
let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

let handle = std::thread::spawn(move || {
    // Should return Err(EventError::Terminated) after the crafted request lands.
    receiver.next_event()
});

let mut stream = TcpStream::connect(addr).unwrap();
stream.write_all(b"POST /shutdown HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: 0\r\n\r\n").unwrap();

let result = handle.join().unwrap();
assert!(matches!(result, Err(EventError::Terminated)));
// receiver.is_stopped() is now true; main_loop would break on the next iteration.
```

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

**File:** libsigner/src/events.rs (L437-446)
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
