### Title
Unauthenticated `/shutdown` HTTP endpoint on `SignerEventReceiver` allows any remote party to terminate signer event processing - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` dispatches purely on the requested URL path with no authentication, secret, or peer-identity check of any kind. Any TCP client that can reach the signer's bound event-receiver socket can send a bare `POST /shutdown` and immediately set `stop_signal` to `true`, causing the signer's event-processing `main_loop` to terminate.

### Finding Description
In `libsigner/src/events.rs`, `next_event` reads an HTTP request and dispatches solely by comparing `request.url()` to string literals (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, `/new_block`), with no verification of request origin, no shared secret, and no header/auth check: [1](#0-0) 

Specifically, the `/shutdown` branch unconditionally stores `true` into the shared `stop_signal` and returns `Err(EventError::Terminated)`: [2](#0-1) 

`EventReceiver::main_loop` treats `EventError::Terminated` as a clean exit signal and breaks out of the loop immediately: [3](#0-2) 

Wait — that citation is from the trait default `main_loop`, confirmed at [4](#0-3) . This is the same loop actually spawned in its own thread for the signer via `Signer::spawn`: [5](#0-4) .

The intended legitimate caller of `/shutdown` is `SignerStopSignaler::send`, which is only invoked in-process when the signer's own runloop decides to exit: [6](#0-5) . However, nothing on the wire distinguishes this legitimate local caller from an arbitrary remote TCP client — the HTTP server (`tiny_http`-backed `HttpServer`) accepts connections from any address that can route to the bound socket, and the handler performs no IP allowlist, no bearer/basic auth, and no shared-secret comparison before honoring `/shutdown`. Confirmed via search: no `Authorization`/`auth_token`/`shared_secret` handling exists anywhere in `libsigner`.

### Impact Explanation
A single unauthenticated POST request permanently halts the signer's event ingestion: `stop_signal` becomes `true`, `is_stopped()` on subsequent checks returns `true`, and `main_loop` exits, tearing down the `event_receiver` thread (`libsigner/src/runloop.rs` spawn wiring). After this, the signer stops receiving StackerDB chunk events, block-validation responses, burn-block events, and new-block events, effectively removing that signer from the network's block-signing quorum until manually restarted. This is a remote crash/unauthenticated DoS achievable from a single crafted message, matching the Critical severity bucket ("remote crash/unauthenticated DoS from few messages").

### Likelihood Explanation
Precondition is only network reachability to the signer's configured event-receiver bind address/port (`config.endpoint`) — no secret, no peer registration, no StackerDB slot ownership, and no privileged role is required. The attacker cost is a single raw HTTP POST, trivially repeatable against every signer whose endpoint is reachable (e.g., not strictly firewalled to localhost/the paired node). The comment in `stacks-signer/src/lib.rs` explicitly warns operators about exposing this endpoint to external networks, underscoring that the code itself provides no protection: [7](#0-6) .

### Recommendation
Require authentication for state-changing endpoints (especially `/shutdown`), e.g., a shared secret/bearer token configured between the node and signer and checked before honoring `/shutdown`, `/stackerdb_chunks`, `/proposal_response`, etc., and/or bind/restrict the event-receiver socket to loopback or an explicit allowlisted peer address, verified at accept-time rather than relying purely on operator-configured firewalling.

### Proof of Concept
Rust integration test sketch:
1. Spawn a `SignerEventReceiver` via `bind()` on `127.0.0.1:0`, start `main_loop` in a thread, and register a consumer channel via `add_consumer`.
2. From a separate "attacker" `TcpStream`, connect to the bound address and write raw bytes:
   `POST /shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\n\r\n`
3. Assert that within a short timeout the spawned `main_loop` thread has exited (e.g., `JoinHandle::join()` completes, or `is_stopped()` returns `true`), and that a subsequent legitimate `POST /stackerdb_chunks` from the "real node" is no longer processed (the consumer channel receives nothing further), demonstrating that one unauthenticated attacker packet permanently disables event ingestion without needing to know any secret or node identity.

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

**File:** libsigner/src/runloop.rs (L228-236)
```rust
        // start a thread for the event receiver
        let event_thread = thread::Builder::new()
            .name(format!("event_receiver:{bind_port}"))
            .stack_size(THREAD_STACK_SIZE)
            .spawn(move || event_receiver.main_loop())
            .map_err(|e| {
                error!("EventReceiver failed to start: {:?}", &e);
                EventError::FailedToStart
            })?;
```

**File:** libsigner/src/runloop.rs (L284-312)
```rust

```

**File:** stacks-signer/src/lib.rs (L125-132)
```rust
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
```
