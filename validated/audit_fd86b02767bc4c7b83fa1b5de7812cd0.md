### Title
Unauthenticated `POST /shutdown` lets any party reaching the signer's event listener kill the event-receiver loop - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` matches the raw URL string `"/shutdown"` and, on match, unconditionally sets `stop_signal` to `true` and returns `EventError::Terminated`, with no signature, HMAC, token, or peer-identity check that the caller is the paired `SignerStopSignaler`/node process. Note the actual code lives in `libsigner/src/events.rs::next_event` (the question's cited path `stackslib/src/net/http/stream.rs` does not contain this logic in this repo), but the vulnerable equality/fault the question describes is present there.

### Finding Description
`SignerEventReceiver::bind` starts a plain `tiny_http::HttpServer` on the configured `endpoint` [1](#0-0) . In `next_event`, requests are dispatched purely by matching `request.url()` string literals, with no authentication of any kind on any branch, including `/shutdown`: on a match, the code does `event_receiver.stop_signal.store(true, Ordering::SeqCst); Err(EventError::Terminated)` [2](#0-1) . `main_loop` treats `EventError::Terminated` as a clean exit signal and `break`s out of the loop, permanently ending event processing [3](#0-2) .

The intended caller is `SignerStopSignaler::send`, which is only supposed to be invoked by the paired signer runloop (via `RunningSigner::stop` or the OS signal handler) and which itself just opens a `TcpStream` to `self.local_addr` and sends the literal HTTP request `POST /shutdown ...` with no secret, signature, or nonce embedded [4](#0-3) . Because the wire format of that "self-shutdown" request is a fixed, unauthenticated string, any party that can open a TCP connection to the bound address and replay/craft the same bytes achieves the exact same effect. There is no check anywhere in this path verifying that the request's origin is the local node/signaler process (no shared secret, no HMAC, no loopback-only enforcement visible in this code, no auth header check).

### Impact Explanation
Any party able to reach the TCP port on which the signer's `SignerEventReceiver` is bound can send one crafted `POST /shutdown` and permanently stop the signer's event-receiver thread (`main_loop` exits, matching `is_stopped() == true`). This halts delivery of all subsequent StackerDB chunk events, burn-block events, block-validation responses, and new-block events into the signer runloop, since `forward_event` is no longer being fed by `next_event`. This is a single-message, repeatable denial of service against the signer's ability to participate in block signing until the process is manually restarted, matching the "Critical: remote crash/unauthenticated DoS from few messages" category — contingent entirely on the attacker being able to reach that specific port.

### Likelihood Explanation
The severity is fully conditioned on reachability of the signer's event-receiver bind address from an unprivileged remote party. This bind address is a signer-local listening socket that the node's event-observer subsystem posts to (configured via the signer's `endpoint` config, typically intended to be bound to `localhost`/an internal address, not the node's public P2P or RPC port). The attacker model defined for this audit is scoped to "a remote party who can connect to a node's P2P or RPC port" — the signer's event-receiver socket is neither of those; it is a separate, operator-configured listener whose network exposure depends entirely on deployment (firewalling/binding choice), which this repository's code does not control. I could not verify within this session whether any default configuration in `stacks-signer/src/config.rs` binds this endpoint to a non-loopback interface by default; this is an operational/deployment fact outside the traced code, not a code-level guarantee, so I can't confirm the reachability precondition holds under the audit's defined attacker model without further investigation of deployment defaults.

If, and only if, this port is exposed to a network-reachable attacker (i.e., the deployer bound it to a non-loopback address), the attack is trivial and repeatable: it requires zero credentials, zero handshake, and a single raw HTTP POST.

### Recommendation
Add authentication to the `/shutdown` endpoint (and ideally all endpoints in `next_event`) — e.g., a shared secret/token known only to the signer process and embedded by `SignerStopSignaler::send`, verified via constant-time comparison before honoring the shutdown request — or, at minimum, enforce that `next_event`'s HTTP server only accepts loopback connections for control endpoints like `/shutdown`, rejecting any request whose peer address is not `127.0.0.1`/`::1`.

### Proof of Concept
Rust test in `libsigner::events`:
1. Construct a `SignerEventReceiver<T>` and call `bind` on `127.0.0.1:0` (or an explicit port), obtaining the bound address.
2. Spawn `main_loop` in a thread.
3. From a plain `TcpStream::connect` (simulating an attacker with no signer credentials), write the exact literal HTTP request used in `SignerStopSignaler::send` (`"POST /shutdown HTTP/1.1\r\nHost: ...\r\nConnection: close\r\nContent-Length: ...\r\n\r\n..."`).
4. Assert that `event_receiver.is_stopped()` becomes `true` and that the `main_loop` thread joins (exits) shortly after, solely as a result of the unauthenticated request, with no `SignerStopSignaler` ever being constructed or used by the test.

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
