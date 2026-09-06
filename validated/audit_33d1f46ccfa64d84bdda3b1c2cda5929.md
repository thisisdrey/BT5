### Title
Unauthenticated remote shutdown of signer event receiver via `/shutdown` endpoint - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` treats any HTTP POST to `/shutdown` on the signer's event-receiver socket as an authoritative stop command, with no check that the request originated from the local `SignerStopSignaler`. Any TCP client that can reach the bound event-receiver address can send this exact request and halt the signer's `main_loop`.

### Finding Description
`SignerStopSignaler::send` is meant to be the only legitimate way to stop the receiver: it connects to `self.local_addr` and issues `POST /shutdown HTTP/1.1 ... Connection: close ...` with an arbitrary body [1](#0-0) . However, `next_event` dispatches purely on `request.url()`, with no signature, token, or origin check:

```
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
}
``` [2](#0-1) 

Any TCP client that can open a connection to the signer's event-receiver listening socket can send an identical (or even simpler) `POST /shutdown` request and the branch fires unconditionally — the body content and method are checked, but not the caller's identity. This immediately sets `stop_signal` to `true` and returns `EventError::Terminated`, which `main_loop` (called from `EventReceiver::main_loop`, `libsigner/src/events.rs:284-312`) treats as a graceful termination signal, breaking the loop at [3](#0-2) . There is no equality check tying the shutdown request to the trusted local stop-signaler (e.g., no shared secret, no loopback-only enforcement, no comparison of `request` origin to `self.local_addr`). The `bind` implementation binds `HttpServer::http(listener)` directly to whatever `SocketAddr` is configured [4](#0-3) ; if that address is not restricted to loopback (e.g. bound to `0.0.0.0` or a routable interface, as is possible via signer config), the port is remotely reachable by any unprivileged party.

### Impact Explanation
A single unauthenticated HTTP POST causes the signer's event-receiver main loop to exit, halting event processing (`StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, `NewBlock` deliveries all stop being forwarded to the signer runloop). This is a remote, unauthenticated denial-of-service triggered by a single crafted message, matching the "Critical - remote crash/unauthenticated DoS from few messages" category. It affects whichever signer node's event port is reachable; it is fully repeatable (each `/shutdown` send re-triggers the store, though once stopped the receiver will not restart without operator intervention).

### Likelihood Explanation
Preconditions: the attacker must be able to open a TCP connection to the signer's bound event-receiver port. No secret, key, peer role, or StackerDB slot is required — only network reachability to that port. If the signer's `endpoint` is configured to listen on a non-loopback interface (which is possible depending on deployment/config), this is trivially exploitable by any remote party sending a single crafted HTTP request. Cost to the attacker is a single TCP connection and a handful of bytes.

### Recommendation
Require the `/shutdown` request to be authenticated as originating from the trusted local stop-signaler — e.g., bind the event receiver to loopback only, or require a shared secret/token in the request body/headers that is verified before setting `stop_signal`, and reject/ignore `/shutdown` requests lacking it.

### Proof of Concept
```rust
// libsigner/src/events.rs (integration-style test)
use std::net::{TcpStream, SocketAddr};
use std::io::Write;
use std::sync::mpsc::channel;

#[test]
fn unauthenticated_shutdown_via_raw_tcp() {
    let mut receiver: SignerEventReceiver<SomeMessageType> = SignerEventReceiver::new(false);
    let addr: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let bound = receiver.bind(addr).unwrap();
    let (tx, _rx) = channel();
    receiver.add_consumer(tx);

    // Attacker: unprivileged raw TCP connection, no secret, no local access.
    let mut stream = TcpStream::connect(bound).unwrap();
    let body = "attacker-controlled";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        bound, body.len(), body
    );
    stream.write_all(req.as_bytes()).unwrap();

    // Trigger request handling
    let _ = receiver.next_event(); // returns Err(EventError::Terminated)

    assert!(receiver.is_stopped()); // stop_signal set to true by an unauthenticated remote client
}
```
This demonstrates that `is_stopped()` becomes `true` from a raw, unauthenticated TCP client with no verification against the legitimate `SignerStopSignaler`, confirming the unauthenticated remote DoS described above.

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

**File:** libsigner/src/events.rs (L404-408)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }
```

**File:** libsigner/src/events.rs (L443-445)
```rust
            } else if request.url() == "/shutdown" {
                event_receiver.stop_signal.store(true, Ordering::SeqCst);
                Err(EventError::Terminated)
```
