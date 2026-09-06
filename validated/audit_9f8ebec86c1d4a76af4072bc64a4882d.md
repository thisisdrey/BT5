There is no authentication mechanism (no secret, token, or `Authorization` header check) gating the `/shutdown` endpoint in `next_event()`.### Title
Unauthenticated remote `/shutdown` request terminates `SignerEventReceiver` main loop - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event` treats any HTTP `POST /shutdown` request matching `request.url() == "/shutdown"` as a valid stop signal, with no secret, token, or peer-identity check. Any TCP peer that can reach the receiver's bound address can replicate `SignerStopSignaler::send`'s exact request byte-for-byte and permanently halt the signer's event-receiving loop.

### Finding Description
In `next_event()` [1](#0-0) , the branch handling `/shutdown` unconditionally calls `event_receiver.stop_signal.store(true, Ordering::SeqCst)` and returns `Err(EventError::Terminated)` as soon as the URL matches, with no validation of the request body, a shared secret, or the sender's identity — the same check used by the legitimate `SignerStopSignaler::send` [2](#0-1) , whose constructed request (`"POST /shutdown HTTP/1.1\r\nHost: ...\r\nContent-Length: ...\r\n\r\n..."`) is fully attacker-replicable since it contains no secret or signature. Once `stop_signal` is set, `EventReceiver::main_loop` observes `is_stopped() == true` on its next iteration and calls `break`, permanently exiting the loop [3](#0-2) . There is no re-arm path — the signer's event-receiver thread stays down until the process is restarted. No authentication mechanism (secret header, token, mTLS, etc.) exists anywhere in this file to gate the endpoint [4](#0-3) .

### Impact Explanation
Any remote party capable of opening a TCP connection to the `SignerEventReceiver`'s bound HTTP address (the signer's event-listener port that the node's event-observer posts to) can permanently stop that signer instance from receiving `stackerdb_chunks`, `proposal_response`, `new_burn_block`, and `new_block` events by sending a single crafted `POST /shutdown` request. This is a single-message, unauthenticated denial of service against a signer's event pipeline — matching the "Critical: remote crash/unauthenticated DoS from few messages" category. It is trivially repeatable (the endpoint stays down until manual restart, and the attack itself requires only one HTTP request).

### Likelihood Explanation
The only precondition is TCP reachability to the bound listener address — no secret, peer key, StackerDB slot, or admin role is required. The attack cost is a single crafted HTTP request identical in structure to what `SignerStopSignaler::send` legitimately sends, which is visible in this open-source file and thus trivially reproducible by any external sender. Whether this address is reachable from the public internet depends on operator deployment/binding choice (`0.0.0.0` vs `127.0.0.1`), but the code itself enforces no network-level or application-level restriction, so any operator who binds it to a non-loopback interface (a supported, non-exotic configuration) is exposed.

### Recommendation
Gate the `/shutdown` endpoint behind an authenticated mechanism: require a shared secret/HMAC token known only to the local `SignerStopSignaler` (e.g., a random per-process token compared with constant-time equality), or restrict the endpoint to loopback-only connections at the transport layer, or replace the HTTP shutdown side-channel entirely with an in-process synchronization primitive (e.g., a self-pipe/condvar) instead of a network-reachable trigger.

### Proof of Concept
Rust test plan (in `libsigner/src/events.rs` test module or an integration test under `libsigner/src/tests/`):
1. Construct a `SignerEventReceiver::<T>::new(false)`, call `bind()` on `127.0.0.1:0`, record the returned `SocketAddr`.
2. Spawn a thread running `main_loop()` (or repeatedly call `next_event()`).
3. From a separate thread/connection (not via `get_stop_signaler()`), open a raw `TcpStream::connect(addr)` and write bytes identical to what `SignerStopSignaler::send` constructs, e.g.:
   ```
   let body = "attacker";
   let req = format!(
       "POST /shutdown HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\nContent-Length: {}\r\nContent-Type: text/plain\r\n\r\n{}",
       body.len(), body
   );
   stream.write_all(req.as_bytes()).unwrap();
   ```
4. Assert that a subsequent call to `next_event()` returns `Err(EventError::Terminated)` and that `event_receiver.is_stopped()` is `true` — i.e. `assert!(receiver.is_stopped())` at the exact site of `stop_signal.store(true, ...)` in `next_event()` (`libsigner/src/events.rs:444`), confirming the receiver halted from an unauthenticated, arbitrary remote connection.

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

**File:** libsigner/src/events.rs (L413-459)
```rust
    fn next_event(&mut self) -> Result<SignerEvent<T>, EventError> {
        self.with_server(|event_receiver, http_server, _is_mainnet| {
            // were we asked to terminate?
            if event_receiver.is_stopped() {
                return Err(EventError::Terminated);
            }
            debug!("Request handling");
            let request = http_server.recv()?;
            debug!("Got request"; "method" => %request.method(), "path" => request.url());

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
            } else {
                let url = request.url().to_string();
                debug!(
                    "[{:?}] next_event got request with unexpected url {}, return OK so other side doesn't keep sending this",
                    event_receiver.local_addr,
                    url
                );
                ack_dispatcher(request);
                Err(EventError::UnrecognizedEvent(url))
            }
        })?
    }
```
