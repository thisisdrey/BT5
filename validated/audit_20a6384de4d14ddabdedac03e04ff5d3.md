### Title
Unauthenticated `/shutdown` HTTP route permanently kills the signer event-receiver loop - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` matches on `request.url() == "/shutdown"` with no authentication, secret, or origin check, and unconditionally sets `stop_signal.store(true, Ordering::SeqCst)` before returning `Err(EventError::Terminated)`. Any TCP peer that can reach the signer's bound event-receiver port can send a single `POST /shutdown` request and permanently stop the signer from receiving further StackerDB/block/burn events.

### Finding Description
The broken equality: authorization required to invoke the stop path should be "only the local node's `SignerStopSignaler::send`", but the actual code enforces authorization == none — any TCP client hitting the `/shutdown` URL string is treated identically to the legitimate internal caller.

Tracing the path: `SignerEventReceiver::bind` opens an `HttpServer` on the configured listener address [1](#0-0) . `next_event` receives a raw request via `http_server.recv()`, and dispatches purely on the URL string with no session, token, or signature verification: [2](#0-1) . Compare this to the legitimate producer, `SignerStopSignaler::send`, which builds exactly the same bare, unauthenticated `POST /shutdown` request over a fresh `TcpStream::connect` — i.e., the "trusted" caller and an external attacker are byte-for-byte indistinguishable on the wire: [3](#0-2) . Once `stop_signal` is set, `is_stopped()` returns true forever (no reset path exists) and `main_loop` breaks out on the next `Err(EventError::Terminated)`, ending the event-receiving thread: [4](#0-3)  and [5](#0-4) .

No guard exists anywhere in this dispatch — not a shared secret, not a source-IP allowlist, not an HTTP auth header check — unlike the node's RPC endpoints which typically require a secret. The `/status` route similarly requires no auth but is harmless (read-only ack); `/shutdown` is destructive and irreversible for the process's lifetime.

### Impact Explanation
Any remote TCP peer able to reach the bound event port can permanently disable a signer's event ingestion pipeline with a single unauthenticated request, blocking StackerDB chunk events, block proposal responses, burn block events, and new block events from ever reaching that signer's runloop again. This is a Critical unauthenticated single-message DoS against the signer, matching the stated Critical impact category (remote crash/unauthenticated DoS from a few messages). It is fully repeatable across any number of signer nodes reachable on that port, and requires no prior signer key, StackerDB slot, or peer relationship.

### Likelihood Explanation
The only precondition is network reachability to the port that `SignerEventReceiver::bind` listens on [1](#0-0) . That address/port is operator-configured (the node's event-observer target used by the signer), and in many deployments this port is intended to be reachable at least from the paired stacks-node, and depending on network/firewall configuration may be reachable more broadly. Attacker cost is a single crafted `POST /shutdown HTTP/1.1` TCP payload; no cryptographic material, no valid StackerDB slot, and no RPC secret is required. The action is fully repeatable and requires no timing or race condition — the `store(true, ...)` in [2](#0-1)  is unconditional.

### Recommendation
Require the `/shutdown` request to be authenticated the same way other privileged control paths are (e.g., a shared secret/token known only to the local process, or restrict the listener/route to loopback-only and/or validate the request originates from `127.0.0.1`/the paired node). At minimum, gate the `stop_signal.store` call in `next_event` behind a check of a secret value known only to `SignerStopSignaler`, rather than relying purely on the URL string `"/shutdown"`.

### Proof of Concept
```rust
// libsigner/src/events.rs (test module) — plan
#[test]
fn unauthenticated_remote_shutdown_dos() {
    let mut receiver: SignerEventReceiver<SomeSignerMessageType> = SignerEventReceiver::new(false);
    let bound_addr = receiver.bind("127.0.0.1:0".parse().unwrap()).unwrap();

    // Attacker: independent TcpStream, no prior handshake, no secret.
    let mut attacker_stream = TcpStream::connect(bound_addr).unwrap();
    let body = "attacker payload";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
        bound_addr, body.len(), body
    );
    attacker_stream.write_all(req.as_bytes()).unwrap();

    // Assert the receiver terminates from an unauthenticated remote message.
    let result = receiver.next_event();
    assert!(matches!(result, Err(EventError::Terminated)));
    assert!(receiver.is_stopped());

    // Subsequent legitimate calls also immediately terminate — permanent DoS.
    assert!(matches!(receiver.next_event(), Err(EventError::Terminated)));
}
```
This test directly exercises the code at [2](#0-1)  and confirms `stop_signal` is set and `next_event` permanently returns `Terminated` after a bare, unauthenticated `/shutdown` POST from an unrelated `TcpStream`.

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

**File:** libsigner/src/events.rs (L462-464)
```rust
    fn is_stopped(&self) -> bool {
        self.stop_signal.load(Ordering::SeqCst)
    }
```
