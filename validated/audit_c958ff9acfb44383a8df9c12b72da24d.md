### Title
Unauthenticated HTTP event ingestion in `SignerEventReceiver` allows anyone reaching the signer's listen port to inject forged node events - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver`, the HTTP server that the `stacks-signer` binds to receive events pushed by a `stacks-node` (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`), accepts and processes any POST request with no authentication check whatsoever. The documentation (`docs/signing.md`) describes an `auth_token`/`auth_password` shared secret that is supposed to gate this channel, but no such check exists anywhere in the transport code that actually services the requests.

### Finding Description
The `EventReceiver` trait's `next_event()` implementation for `SignerEventReceiver` reads an incoming HTTP request and dispatches purely on URL path, with zero credential/token verification: [1](#0-0) 

`bind()` simply opens a plain `tiny_http`-style server on the configured socket with no auth middleware: [2](#0-1) 

The `process_event` helper only reads the body and JSON-deserializes it into a `SignerEvent`; it never inspects headers for a token/password: [3](#0-2) 

Yet operator-facing documentation explicitly instructs users to configure a shared secret (`auth_token` on the node side, `auth_password` on the signer side) and states these "must match" as a security boundary: [4](#0-3) 

A search across `libsigner/**` for any check of `auth_password`/`Authorization` found nothing — the field referenced in documentation is not wired into the actual `SignerEventReceiver`/`process_event` transport path that services HTTP requests. The bypass here is structurally the same as CVE-2018-7749: a documented/expected authentication gate is never actually enforced before the server processes and forwards the request payload.

`SpawnedSigner::new` binds the receiver directly to the configured `endpoint` (which the sample configs bind to `0.0.0.0:<port>`) without layering in any authentication: [5](#0-4) 

Because there is no verification of sender identity, any network entity that can reach the signer's bound port can POST directly to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` and have the payload accepted as if it came from the trusted local `stacks-node`, entirely bypassing the node's own StackerDB chunk signature checks (which live upstream, inside `stackslib`, and are never re-applied here) or any node-originated provenance.

### Impact Explanation
This breaks the "authenticated (came from my paired node) vs. arbitrary sender" equality that the signer's event ingestion is supposed to enforce. An attacker with network access to the signer's bind address (documented as potentially reachable beyond localhost) can:
- Forge `StackerDBChunksEvent` payloads with attacker-chosen `modified_slots` data, injecting data into the signer's internal event stream without ever passing through the node's StackerDB slot-signature verification (`SlotMetadata::verify`, `validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs`), since this HTTP path is a direct shortcut around that logic.
- Forge `BlockValidateResponse`, `NewBurnBlock`, or `NewBlock` events, feeding spoofed chain-state information into the running signer process.
This is an unauthenticated write into the signer's local event/control-plane, satisfying the Critical bar of "unauthenticated/unauthorized write to state" and "auth bypass" listed in the rules for this scan, since it is is a genuine authentication gate that is documented but not implemented in the transport code, not merely a design choice.

### Likelihood Explanation
High for any signer deployment that trusts documentation and does not additionally firewall the `endpoint` bind address at the network layer — the vulnerability requires no privileged access, no valid key, and no complex race condition; a single crafted HTTP POST suffices. The primary mitigating factor is that operators are advised to bind to localhost/loopback, but the documented `auth_password`/`auth_token` mechanism (which exists in config parsing, per `stacks-signer/src/config.rs`) implies a defense-in-depth expectation that is not actually honored by the code that services requests, making misconfiguration (e.g., binding on a routable interface, as the sample configs literally show binding `0.0.0.0`) directly exploitable.

### Recommendation
Enforce the documented shared-secret check inside `SignerEventReceiver::next_event`/`process_event` (e.g., compare an `Authorization` header against the configured `auth_password`) before accepting and forwarding any event, and reject/close connections lacking a valid credential. Alternatively, if the shared-secret feature was fully removed by design, update `docs/signing.md` to stop advertising it as a security control so operators do not falsely rely on it, and clearly document that binding must be restricted to a trusted/loopback interface.

### Proof of Concept
1. Configure and run a `stacks-signer` per `docs/signing.md`, bound per its sample config (`endpoint = "0.0.0.0:30000"`), with `auth_password` set.
2. From a separate, unauthenticated network host, send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"contract_id": "...", "modified_slots": [ ... attacker-controlled chunk data ... ]}
```
No `Authorization`/`auth_password` header is required or checked; `next_event()` (`libsigner/src/events.rs:413-458`) routes it straight to `process_event::<T, StackerDBChunksEvent>` and forwards the resulting `SignerEvent` to the signer's runloop as if it legitimately originated from the paired `stacks-node`.

### Citations

**File:** libsigner/src/events.rs (L404-408)
```rust
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

**File:** docs/signing.md (L31-59)
```markdown
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]

[connection_options]
auth_token = "your-secret-token"
```

### 2. Configure the Signer

Use [`mainnet-signer-conf.toml`](../sample/conf/signer/mainnet-signer-conf.toml) as a starting point.
Key settings:

```toml
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```

### 3. Verify Coordination

These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
| `node_host`     | `[node] rpc_bind`                 | Signer connects to node's RPC |
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
