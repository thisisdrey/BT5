### Title
Unauthenticated remote `POST /shutdown` permanently terminates `SignerEventReceiver::main_loop` - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` dispatches any HTTP `POST` to `/shutdown` — regardless of sender — into an unconditional `stop_signal.store(true, Ordering::SeqCst)` followed by `Err(EventError::Terminated)`. There is no check that the request originated from the signer's own `SignerStopSignaler` (loopback, self-issued) versus an arbitrary remote TCP client, so any party able to reach the event-receiver's bound port can permanently stop the signer's event stream with a single request.

### Finding Description
The claimed equality — "the `/shutdown` request received == a shutdown command issued by the node operator's own `SignerStopSignaler`" — is false in the code. `SignerStopSignaler::send` simply opens a `TcpStream::connect` to the receiver's `local_addr` and writes a raw HTTP request `POST /shutdown ...` [1](#0-0) . Nothing in that payload (no secret, no token, no signature) is verified by the receiver. `next_event` matches purely on `request.url() == "/shutdown"` and, on match, immediately does `event_receiver.stop_signal.store(true, Ordering::SeqCst); Err(EventError::Terminated)` [2](#0-1) . Any TCP client that can complete the HTTP request line/headers against the bound `HttpServer` (created via `bind()` at [3](#0-2) ) triggers the identical code path — the method only checks `POST` and the URL path, both attacker-controlled and trivially reproducible.

Once `Err(EventError::Terminated)` propagates up, `EventReceiver::main_loop` matches that variant and `break`s out of its loop, ending event forwarding permanently [4](#0-3) . Because `stop_signal` is a shared `Arc<AtomicBool>` and there is no reset path, this is a durable/irrecoverable stop rather than a transient one; a fresh event never restarts `main_loop`.

No guard exists to distinguish this from a legitimate call: the "loopback-only" property of `SignerStopSignaler` is merely a convention of how the signer's own runloop uses it (connecting to its own `local_addr`), not an enforced restriction on the underlying `HttpServer`, which is bound to whatever socket address is configured via `EventReceiver::bind` and accepts connections from any source able to reach that port.

### Impact Explanation
A remote, unprivileged attacker who can reach the signer's event-receiver port can permanently stop that signer instance from receiving `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` events with a single, non-volumetric HTTP POST. This is an unauthenticated denial-of-service against the signer's event pipeline — the signer process keeps running but stops reacting to node-driven events (miner block proposals, stacker-DB gossip, burn/stacks block events), effectively silencing that signer's participation, matching the "Critical: remote crash/unauthenticated DoS from few messages" impact category. The blast radius is scoped to whichever signer instance's event-receiver port is reachable by the attacker.

### Likelihood Explanation
Preconditions are minimal: the attacker only needs network reachability to the TCP port that the signer's `SignerEventReceiver::bind` listens on (this is configured by node operators, and whether it is bound to loopback-only or a wider interface is an operational/config decision, not enforced in this code). The attack requires no secret, no valid peer key, no StackerDB slot ownership, and is a single crafted HTTP request — cost is essentially zero and fully repeatable against any exposed instance.

### Recommendation
Require the `/shutdown` handler to authenticate the caller before honoring it — e.g., bind the event-receiver's control channel to loopback only and enforce it in code (reject non-loopback peer addresses at the `tiny_http` request/connection level), and/or require a shared secret/token embedded by `SignerStopSignaler` and validated by `next_event` before calling `stop_signal.store(true, ...)`. At minimum, verify `request.remote_addr()` is loopback prior to acting on `/shutdown`.

### Proof of Concept
```rust
// libsigner/src/events.rs (or an integration test crate)
use std::net::{TcpStream, SocketAddr};
use std::io::Write;
use std::sync::mpsc::channel;
use std::thread;

#[test]
fn remote_shutdown_terminates_main_loop() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> = SignerEventReceiver::new(false);
    let addr: SocketAddr = "127.0.0.1:0".parse().unwrap(); // operator may bind wider
    let bound = receiver.bind(addr).unwrap();
    let (tx, _rx) = channel();
    receiver.add_consumer(tx);

    // Simulate an arbitrary remote attacker, NOT SignerStopSignaler
    let handle = thread::spawn(move || {
        let mut stream = TcpStream::connect(bound).unwrap();
        let body = "attacker payload";
        let req = format!(
            "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
            bound, body.len(), body
        );
        stream.write_all(req.as_bytes()).unwrap();
    });

    // Directly assert next_event returns Terminated from the crafted, unauthenticated request
    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));

    // main_loop, if run, would now break permanently:
    // receiver.main_loop(); // exits immediately, never forwards future events
    handle.join().unwrap();
}
```
This reproduces the exact code path at [2](#0-1)  and the resulting `break` in [4](#0-3) , using a plain `TcpStream` rather than `SignerStopSignaler::send`, confirming no authentication distinguishes the two callers.

### Citations

**File:** libsigner/src/events.rs (L296-300)
```rust
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
```

**File:** libsigner/src/events.rs (L376-395)
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
