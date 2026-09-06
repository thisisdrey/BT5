### Title
Unauthenticated Event-Ingestion Endpoint Lets Any Reachable Peer Inject Forged Node Events into the Signer - (File: `libsigner/src/events.rs`)

### Summary
The `SignerEventReceiver` that the `stacks-signer` binary uses to ingest events from its co-located Stacks node exposes a plain, unauthenticated HTTP server. Any TCP client that can reach the bound socket can POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` and have the payload deserialized and forwarded into the signer runloop exactly as if it had originated from the trusted node process, with no shared secret, signature, or peer-identity check performed by the transport layer itself.

### Finding Description
`SignerEventReceiver::bind()` opens a raw `tiny_http::HttpServer` on the configured `event_bind` address [1](#0-0) . Its `next_event()` handler dispatches purely on the URL path with no authentication of the caller: it reads the body, converts it to JSON, and returns the resulting `SignerEvent` to the runloop [2](#0-1) . The generic `process_event` helper performs no verification of the sender at all — it just deserializes whatever JSON body was posted into the expected event type and converts it into a `SignerEvent` [3](#0-2) .

This is the same "equality that should be enforced but is not" pattern as the referenced report: the code implicitly assumes "HTTP POST received on this socket" is equivalent to "authentic event from my own Stacks node," the same way `PoolTemplate.initialize()` implicitly assumed "value stored in an approved `_references` slot" was equivalent to "value supplied by the party who should be charged." Nothing in this transport ties the request to the node process (no token, no mTLS, no loopback enforcement in code — the bind address is operator-configurable and the code itself places no restriction on it).

Note that once inside the signer, some event types receive downstream cryptographic checks (e.g., StackerDB chunk contents recovered via `chunk.recover_pk()` and matched against a known signer set in `stackslib/src/net/stackerdb/mod.rs`/`stacks-node/src/nakamoto_node/stackerdb_listener.rs`), but other event kinds — `BlockValidationResponse` (from `/proposal_response`) and `NewBurnBlock`/`NewBlock` — carry no independent cryptographic authentication at all; they are trusted purely because they arrived at this HTTP endpoint.

### Impact Explanation
If the signer's event-receiver socket is reachable by anyone other than the local trusted node (e.g., misconfiguration, container/network exposure, or any co-tenant on the host/network), a remote unprivileged actor can forge `BlockValidateResponse`, `NewBurnBlock`, or `NewBlock` events and inject them directly into the signer's decision pipeline as though the node produced them, without needing any key material. This is an unauthenticated write into the signer's local processing state — matching the "Critical: unauthenticated/unauthorized write to state" bar in the rubric, since the transport itself performs no identity check before treating attacker-supplied bytes as trusted node telemetry.

### Likelihood Explanation
Likelihood depends on network exposure of the configured `event_bind` address, which is an operator-controlled configuration value rather than something the code forces to loopback. The code provides no defense-in-depth (no token, no allowlist, no TLS) if an operator's bind address is broader than `127.0.0.1`, or if the loopback interface is otherwise reachable (e.g., via port-forwarding, shared network namespaces in containers, or SSRF from another local service). Given the code path enforces nothing, exploitability is entirely a function of deployment topology, which I could not fully verify from the indexed config samples.

### Recommendation
Have the event-receiver transport authenticate the sender independently of network topology: require a shared bearer token or HMAC over the request body (configured out-of-band between the node and signer), and/or bind by default to loopback with an explicit opt-in flag to widen exposure. At minimum, reject requests whose source address is not loopback unless an explicit shared secret is presented, and document/enforce that `event_bind` must not be exposed on non-loopback interfaces without additional authentication.

### Proof of Concept
1. Configure a `stacks-signer` instance and note the `event_bind` address it listens on (`SignerEventReceiver::bind`, `libsigner/src/events.rs:404-408`).
2. From any host that can reach that socket, send:
```
POST /new_burn_block HTTP/1.1
Host: <event_bind>
Content-Type: application/json
Content-Length: <n>

{ "burn_block_hash": "...", "burn_block_height": 999999, "consensus_hash": "...", "burn_amount": 0, "reward_recipients": [], "reward_slot_holders": [], "burn_block_time": 0 }
```
3. `process_event::<T, BurnBlockEvent>` deserializes this body and returns it as `SignerEvent::NewBurnBlock` with no check that it came from the actual node process [3](#0-2) ; it is forwarded to the signer runloop via `forward_event` [4](#0-3)  as if genuine.

### Citations

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

**File:** libsigner/src/events.rs (L410-458)
```rust
    /// Wait for the node to post something, and then return it.
    /// Errors are recoverable -- the caller should call this method again even if it returns an
    /// error.
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

**File:** libsigner/src/events.rs (L469-490)
```rust
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

**File:** libsigner/src/events.rs (L519-542)
```rust
fn process_event<T, E>(mut request: HttpRequest) -> Result<SignerEvent<T>, EventError>
where
    T: SignerEventTrait,
    E: serde::de::DeserializeOwned + TryInto<SignerEvent<T>, Error = EventError>,
{
    let mut body = String::new();

    if let Err(e) = request.as_reader().read_to_string(&mut body) {
        error!("Failed to read body: {:?}", &e);
        ack_dispatcher(request);
        return Err(EventError::MalformedRequest(format!(
            "Failed to read body: {:?}",
            e
        )));
    }
    // Regardless of whether we successfully deserialize, we should ack the dispatcher so they don't keep resending it
    ack_dispatcher(request);
    let json_event: E = serde_json::from_slice(body.as_bytes())
        .map_err(|e| EventError::Deserialize(format!("Could not decode body to JSON: {:?}", e)))?;

    let signer_event: SignerEvent<T> = json_event.try_into()?;

    Ok(signer_event)
}
```
