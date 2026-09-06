## Analysis

The reported CVE describes a Jenkins endpoint that lacked CSRF/POST protection, letting an unauthenticated request trigger unintended server-side behavior. The closest structural analog in this repo is not in `stackslib/src/net/api/**` (those endpoints — `postblock_proposal.rs`, `blockreplay.rs`, `txsimulate.rs` — all correctly gate state-changing/sensitive actions behind an `authorization` header check before doing anything). The real analog is the **signer's own inbound HTTP event listener** in the in-scope `libsigner` transport code, which has **no authentication check whatsoever** on any of its routes, including one that immediately terminates the receiver.

### Title
Unauthenticated remote shutdown and forged-event injection on the stacks-signer event listener — (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` dispatches incoming HTTP requests (`/status`, `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/shutdown`) purely by URL path and HTTP verb, with **no authentication of the caller**. `/shutdown` sets the stop signal and terminates the event-receiver loop on a bare POST. The reference/sample signer configuration explicitly binds this listener to `0.0.0.0`, making it reachable from the network.

### Finding Description
The signer's HTTP listener is set up in `bind()`: [1](#0-0) 

and its request dispatch logic checks only the URL and verb, never any credential: [2](#0-1) 

Note in particular:
```
} else if request.url() == "/shutdown" {
    event_receiver.stop_signal.store(true, Ordering::SeqCst);
    Err(EventError::Terminated)
```
Any POST to `/shutdown` sets `stop_signal`, which is checked at the top of `main_loop`, causing the receiver thread to exit permanently: [3](#0-2) 

This is the same mechanism the signer itself uses internally to shut down via `SignerStopSignaler::send()` (which POSTs `/shutdown` from localhost) — but the production HTTP handler doesn't distinguish this from an external, unauthenticated caller: [4](#0-3) 

Crucially, none of the other routes (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`) require the `auth_password`/`auth_token` that the docs describe as securing "node ↔ signer" communication: [5](#0-4) 

That `auth_password`/`auth_token` pair only protects the **signer → node** direction (the node's `/v3/block_proposal` RPC endpoint checks it): [6](#0-5) 

but the **node → signer** direction (this event listener) has no equivalent check at all. The equality that should hold — "only the paired stacks-node can post signer events" — is broken: any remote unprivileged party who can reach the bound port can post to these paths.

The reference deployment configuration binds this exact listener to all interfaces: [7](#0-6) 

`stacks-signer/src/config.rs` accepts an arbitrary `host:port` string for `endpoint` with no enforcement of a loopback-only bind: [8](#0-7) 

### Impact Explanation
This satisfies the "Critical — remote crash/unauthenticated DoS from few messages" bar: a single unauthenticated `POST /shutdown` (no body needed) permanently kills the signer's event-receiver thread, so the signer stops receiving `stackerdb`, `block_proposal`, and `burn_blocks` events from its node — silently taking the signer offline for block validation/signing until manually restarted. Additionally, `/proposal_response` and `/new_burn_block` accept attacker-supplied JSON with no signer-address/signature check at the HTTP layer (unlike `/stackerdb_chunks`, whose payload is later re-verified per-chunk via `chunk.recover_pk()`), so a reachable listener also permits forged-event injection into the signer runloop from an unauthenticated network peer.

### Likelihood Explanation
Directly exploitable if the signer's `endpoint` is bound to a non-loopback interface, which is exactly what the shipped reference config (`sample/conf/signer/mainnet-signer-conf.toml`) and its accompanying docs (`docs/signing.md`) instruct operators to do (`endpoint = "0.0.0.0:30000"`). Requires no authentication, no valid signature, and no special network position — one TCP connection and one crafted HTTP request.

### Recommendation
Require the same `auth_password`/shared secret on all inbound requests to `SignerEventReceiver` (not just outbound signer→node calls), reject `/shutdown` and event-posting routes from anything but authenticated/loopback callers, and update sample configs/docs to bind the listener to `127.0.0.1` by default with an explicit opt-in warning for exposing it externally.

### Proof of Concept
1. Deploy a signer with `endpoint = "0.0.0.0:30000"` per the shipped sample config.
2. From a remote unauthenticated host: `curl -X POST http://<signer-ip>:30000/shutdown`.
3. Observe (per `SignerEventReceiver::next_event`/`main_loop`) that the event-receiver thread exits with `EventError::Terminated`, and the signer stops processing further node events until restarted.

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

**File:** libsigner/src/events.rs (L401-408)
```rust
    /// Start listening on the given socket address.
    /// Returns the address that was bound.
    /// Errors out if bind(2) fails
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
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

**File:** stacks-signer/src/config.rs (L294-300)
```rust
    /// The local endpoint the signer will listen on for events from the Stacks node.
    /// ---
    /// @default: (required, no default)
    /// @notes:
    ///   - Format: `"host:port"` (e.g., `"0.0.0.0:30000"`).
    ///   - Must match the `endpoint` in the node's `[[events_observer]]` section.
    pub endpoint: String,
```

**File:** stacks-signer/src/config.rs (L320-327)
```rust
    /// The authorization password for the block proposal endpoint.
    /// ---
    /// @default: (required, no default)
    /// @notes:
    ///   - WARNING: Must match the `auth_token` in the Stacks node's
    ///     `[connection_options]` section. If these do not match, the signer
    ///     cannot communicate with the node.
    pub auth_password: String,
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1136-1144)
```rust
        let Some(password) = &self.auth else {
            return Err(Error::Http(400, "Bad Request.".into()));
        };
        let Some(auth_header) = preamble.headers.get("authorization") else {
            return Err(Error::Http(401, "Unauthorized".into()));
        };
        if auth_header != password {
            return Err(Error::Http(401, "Unauthorized".into()));
        }
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```
