Confirmed: the `SignerEventReceiver` in `libsigner/src/events.rs` performs no authentication whatsoever on its HTTP endpoint. There's also an explicit warning in `stacks-signer/src/lib.rs` acknowledging this risk, but only as a documentation note, not an enforced control.

### Title
Unauthenticated Signer Event-Receiver HTTP Endpoint Allows Forged StackerDB/Block-Event Injection - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver`, the HTTP server every `stacks-signer` process runs to receive events from its paired Stacks node, accepts and processes POST requests to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` from **any** TCP client that can reach the bound socket — there is no check of an `auth_password`/`Authorization` header or any other credential before the body is parsed and forwarded into the signer's runloop.

### Finding Description
The `bind()` method just opens a plain HTTP listener on the configured `endpoint` with no auth wiring: [1](#0-0) 

`next_event()` dispatches purely on URL path and HTTP method, with zero credential verification, before calling `process_event`, which reads the body, JSON-deserializes it into the target event type, and converts it into a `SignerEvent` that is pushed straight to the signer's channel: [2](#0-1) [3](#0-2) 

A `grep` for `auth_password`/`Authorization`/`authorization` inside `libsigner/**` returns no matches at all — the concept simply is not implemented on the receive path. This is in stark contrast to the node's `auth_token`/`auth_password` pairing that is documented as securing "the communication channel between this node and a connected `stacks-signer` instance" and is actually enforced on the *node's* `/v3/block_proposal` RPC endpoint (see `stackslib/src/net/api/postblock_proposal.rs`, tested in `stackslib/src/net/api/tests/postblock_proposal.rs:62-133`, which returns 401 without the correct `authorization` header). The signer's own listener has no equivalent check, so the "auth-gate" that operators are told exists (per `docs/signing.md` and the sample configs) simply fails open on the signer side.

The sample configs make the exposure concrete: `sample/conf/signer/mainnet-signer-conf.toml` documents `endpoint = "0.0.0.0:30000"` — i.e., binding on all interfaces — and `stacks-signer/src/lib.rs` even prints a runtime warning acknowledging the risk of exposing this endpoint, but does not enforce any mitigation: [4](#0-3) 

Because the endpoint accepts unauthenticated `StackerDBChunksEvent` bodies containing arbitrary `StackerDBChunkData` (`slot_id`, `slot_version`, `sig`, `data`), an attacker who can reach the port can submit any JSON payload matching that shape. While the signer's core decision logic that eventually validates `chunk.recover_pk()` against expected signer identities is out of scope for this analysis, the injection point itself — accepting and forwarding externally supplied "node events" without any origin authentication — is squarely a transport-layer defect in `libsigner`.

### Impact Explanation
Any host that can reach the signer's bound port (which per the shipped sample config is `0.0.0.0:<port>`, i.e., not loopback-restricted) can act as if it were the paired Stacks node: injecting forged `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent` payloads directly into the signer's event stream. This is a network-reachable unauthenticated write into the signer process's input pipeline — matching the "unauthenticated/unauthorized write to state" and "forged gossip relayed" impact classes, since the very messages that are supposed to originate exclusively and authentically from the paired node (protected on the node side by `auth_token`) can be fabricated by anyone with network access to the signer's listening socket.

### Likelihood Explanation
Likelihood is high in any deployment where the signer's `endpoint` is reachable beyond `localhost` — which the project's own reference configuration (`sample/conf/signer/mainnet-signer-conf.toml`) explicitly documents as `0.0.0.0:30000`. No credentials, cryptographic proof, or IP allow-list are required; a single crafted HTTP POST suffices.

### Recommendation
Add an authentication check (e.g., a shared-secret `Authorization` header, mirroring the node's `auth_token` scheme already used for `/v3/block_proposal`) to `SignerEventReceiver::next_event`/`process_event` in `libsigner/src/events.rs` before parsing and forwarding any event body. Alternatively/additionally, default-bind the endpoint to loopback and document that binding to a non-loopback address requires a reverse proxy that enforces authentication.

### Proof of Concept
1. Start a `stacks-signer` configured per `sample/conf/signer/mainnet-signer-conf.toml` with `endpoint = "0.0.0.0:30000"`.
2. From any other host with network access to port 30000, send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"contract_id": "...", "modified_slots": [{"slot_id":0,"slot_version":1,"sig":"<any-hex-sig>","data":"<hex>"}]}
```
3. Observe (as in `libsigner/src/tests/mod.rs:test_simple_signer`, which uses exactly this raw-socket technique) that the request is accepted, JSON-parsed, and forwarded into the signer runloop with no credential check of any kind — confirmed by the complete absence of `auth_password`/`Authorization` handling anywhere under `libsigner/`.

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

**File:** stacks-signer/src/lib.rs (L119-138)
```rust
impl<S: Signer<T> + Send + 'static, T: SignerEventTrait + 'static> SpawnedSigner<S, T> {
    /// Create a new spawned signer
    pub fn new(config: GlobalConfig) -> Self {
        let endpoint = config.endpoint;
        info!("Stacks signer version {:?}", VERSION_STRING.as_str());
        info!("Starting signer with config: {:?}", config);
        warn!(
            "Reminder: The signer is primarily designed for use with a local or subnet network stacks node. \
            It's important to exercise caution if you are communicating with an external node, \
            as this could potentially expose sensitive data or functionalities to security risks \
            if additional proper security checks are not integrated in place. \
            For more information, check the documentation at \
            https://docs.stacks.co/guides-and-tutorials/running-a-signer#preflight-setup"
        );
        let (res_send, res_recv) = channel();
        let ev = SignerEventReceiver::new(config.network.is_mainnet());
        crate::monitoring::actions::start_serving_monitoring_metrics(config.clone()).ok();
        let runloop = RunLoop::new(config.clone());
        let mut signer: RunLoopSigner<S, T> = libsigner::Signer::new(runloop, ev, res_send);
        let running_signer = signer.spawn(endpoint).expect("Failed to spawn signer");
```
