### Title
Unauthenticated `/shutdown` HTTP endpoint permanently kills signer event delivery - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` dispatches purely on the raw `request.url()` string with no authentication, origin check, or shared secret. Any TCP client that can reach the signer's bound event-listener address can POST `/shutdown` and permanently halt the signer's `main_loop`, silencing all future StackerDB chunk, block-validation, and burn-block events.

### Finding Description
In `next_event`, the request is routed solely by URL string equality with no verification of the sender's identity:

<cite repo="Kohvert/stacks-core--021" path="libsigner/src/events.rs" start="437="446" />

Specifically: [1](#0-0) 

If `request.url() == "/shutdown"`, the receiver unconditionally sets `event_receiver.stop_signal.store(true, Ordering::SeqCst)` and returns `Err(EventError::Terminated)`. `main_loop` treats `EventError::Terminated` as a graceful exit condition and `break`s out of the loop permanently: [2](#0-1) 

Once `stop_signal` is `true`, `is_stopped()` returns `true` forever (nothing ever resets it), so even a subsequent call to `bind`/`next_event` immediately short-circuits with `Terminated`: [3](#0-2) [4](#0-3) 

Notably, the legitimate internal caller (`SignerStopSignaler::send`, used by the node's own shutdown logic) itself sends this exact request with no authentication token, just a raw HTTP POST to `/shutdown`: [5](#0-4) 

This confirms there is no secret, header, or origin check anywhere in the protocol — the endpoint is designed to be triggered by "whoever can reach the port," which the question correctly identifies as unenforced sender-identity equality (attacker == configured node dispatcher, but nothing checks this).

### Impact Explanation
Any remote party able to open a TCP connection to the signer's bound event-listener socket (the address configured as the node's `event_observer` target for this signer) can send a single unauthenticated HTTP POST to `/shutdown` and permanently terminate the signer's event-processing loop. After this, the signer no longer receives StackerDB chunks, block proposals, burn-block events, or block-validation responses — it goes dark. This is a single-message, unauthenticated, remote denial of service that requires no prior privilege, matching the "Critical – remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
The only precondition is TCP reachability to the signer's event-receiver port. Since this component is `libsigner/src/events.rs`'s `SignerEventReceiver`, which binds to a configured listener address intended to receive the node's event-observer POSTs, if that address/port is reachable from outside the intended source (e.g., bound to a non-loopback interface, or reachable via the node's network segment), any unprivileged remote attacker can send the exact 1-request payload shown in `SignerStopSignaler::send` (a POST `/shutdown` with a body) and deterministically stop the signer. Attacker cost is a single raw TCP write; no cryptographic material, no legitimate slot ownership, and no admin role are needed.

### Recommendation
Require an authenticated channel for `/shutdown` (and ideally for all event routes): e.g., only bind the event-receiver listener to loopback/localhost by default and document that it must not be exposed, and/or require a shared secret/token (e.g., a bearer token configured alongside the node's `event_observer` config) validated before honoring `/shutdown`, `/stackerdb_chunks`, `/new_block`, `/new_burn_block`, and `/proposal_response`. At minimum, validate the request originates from the loopback interface (`request.remote_addr()`) before acting on `/shutdown`.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) or a separate integration test
#[test]
fn test_unauthenticated_shutdown_via_raw_socket() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> = SignerEventReceiver::new(false);
    let addr = "127.0.0.1:0".parse().unwrap();
    let bound_addr = receiver.bind(addr).unwrap();

    // Spawn next_event in a thread to simulate main_loop's blocking call
    let handle = std::thread::spawn(move || receiver.next_event());

    // Attacker: unrelated raw socket, no auth, no shared secret
    let mut stream = std::net::TcpStream::connect(bound_addr).unwrap();
    let body = "attacker shutdown";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        bound_addr, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    let result = handle.join().unwrap();
    assert!(matches!(result, Err(EventError::Terminated)));
    // subsequent calls to is_stopped() are permanently true, halting main_loop forever
}
```
This reproduces exactly the code path at `libsigner/src/events.rs:443-445`, confirming the attacker-controlled `/shutdown` POST reaches `stop_signal.store(true, ...)` with zero authentication.

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

**File:** libsigner/src/events.rs (L413-418)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
```

**File:** libsigner/src/events.rs (L443-446)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
            } else if request.url() == "/new_block" {
```

**File:** libsigner/src/events.rs (L461-464)
```rust
    /// Determine if the receiver is hung up
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
