### Title
Unauthenticated `/shutdown` POST permanently halts signer event receiver (DoS) - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event()` routes any POST to `/shutdown` on the event socket to the same code path used internally by `SignerStopSignaler`, setting the stop flag and returning `EventError::Terminated` with no check on the sender's identity. Any remote party able to reach the signer's event listener can send a single unauthenticated HTTP POST to permanently terminate the event receiver thread.

### Finding Description
The route dispatch in `next_event` treats `/shutdown` identically regardless of caller: [1](#0-0) 
This is the exact same action that `SignerStopSignaler::send()` performs by connecting to `self.local_addr` and issuing a plaintext `POST /shutdown` with no token or credential: [2](#0-1) 
There is no secret, signature, or origin check tying this call to the local signer process — the equality the code enforces is "anyone who can POST /shutdown" == "the legitimate internal stop signaler," which is false. The `EventError::Terminated` propagates through `main_loop`'s match arm, which explicitly breaks the loop: [3](#0-2) [4](#0-3) 
Once stopped, `is_stopped()` will also short-circuit any future call to `next_event` with the same `Terminated` error: [5](#0-4) 
No other guard in this file (no auth header check, no local-address/loopback check, no token comparison) is applied before reaching this branch — the request only needs `method() == POST` and `url() == "/shutdown"`.

### Impact Explanation
A single crafted TCP message permanently halts the signer's event-receiver thread, meaning the signer stops ingesting `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` events from the node until the process is manually restarted. This is a control-plane action (shutdown) triggerable by an unprivileged remote party with a single message — matching the "Critical: remote crash/unauthenticated DoS from few messages" category.

### Likelihood Explanation
Precondition is only that the attacker can open a TCP connection to the signer's event listener port (same reachability precondition as the other unauthenticated endpoints in this file, e.g., `/stackerdb_chunks`, `/new_burn_block`). No secret, key, or StackerDB slot ownership is required. The exploit is a single, cheap, deterministic HTTP request — fully repeatable against any signer exposing this port.

### Recommendation
Require the shutdown request to be authenticated as originating from the local process — e.g., bind the event listener strictly to loopback and additionally require a random per-process shared secret/token generated at `bind()` time and known only to `SignerStopSignaler`, checked in the `/shutdown` branch before flipping `stop_signal`. Alternatively, replace the HTTP-based self-signal with a proper in-process mechanism (e.g., a secondary local-only Unix socket, or waking the `HttpServer::recv` blocking call via a non-network mechanism) so no network-reachable endpoint can trigger shutdown.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module)
use std::io::Write;
use std::net::TcpStream;

#[test]
fn test_unauthenticated_shutdown_dos() {
    let mut receiver: SignerEventReceiver<crate::v0::messages::SignerMessage> =
        SignerEventReceiver::new(false);
    let addr = "127.0.0.1:0".parse().unwrap();
    let bound = receiver.bind(addr).unwrap();

    // Spawn a thread to run next_event once
    let handle = std::thread::spawn(move || receiver.next_event());

    // Attacker: raw TCP connection, no credentials, no relation to SignerStopSignaler
    let mut stream = TcpStream::connect(bound).unwrap();
    let req = "POST /shutdown HTTP/1.1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
    stream.write_all(req.as_bytes()).unwrap();

    let result = handle.join().unwrap();
    assert!(matches!(result, Err(EventError::Terminated)));
    // subsequent calls will also always return Err(Terminated), confirming permanent halt
}
```
This demonstrates that an unrelated `TcpStream` — not `SignerStopSignaler` — can trigger `Err(EventError::Terminated)` and permanently flip `stop_signal`, with no credential exchanged.

### Citations

**File:** libsigner/src/events.rs (L296-300)
```rust
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
```

**File:** libsigner/src/events.rs (L311-311)
```rust
        info!("Event receiver main loop exit");
```

**File:** libsigner/src/events.rs (L382-394)
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
        }
```

**File:** libsigner/src/events.rs (L416-418)
```rust
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```
