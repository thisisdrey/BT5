### Title
Signer event-receiver HTTP endpoint accepts StackerDB/block/burn-block events from any unauthenticated caller - (File: libsigner/src/events.rs)

### Summary

### Finding Description
`SignerEventReceiver::next_event()` binds a plain `tiny_http` server and dispatches any inbound POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` straight into `process_event::<T, E>()`, which only checks the JSON body's shape before converting it into a `SignerEvent` and forwarding it to the signer runloop [1](#0-0) . There is no check of any shared secret, bearer token, or source-IP allowlist anywhere in this dispatch path — the only per-request handling is method/URL matching [2](#0-1) . `docs/signing.md` documents an `auth_password`/`auth_token` pair that is supposed to gate this coordination channel, and states they "must match", but that value lives in the *node's* `connection_options.auth_token` used for the node's own outbound HTTP dispatch conventions, not as an inbound check enforced by `SignerEventReceiver` [3](#0-2) . Searching the whole repository for any `Authorization`/`auth_token` verification logic inside `libsigner` turns up nothing — the config value is only referenced in sample TOML files and in `stacks-signer/src/config.rs`/`stacks_client.rs` (i.e., only used when the signer talks *out* to the node's RPC API), never when the signer's own event listener validates an *incoming* connection.

This is the direct analog of the reported bug class: an entity ("the node") is implicitly trusted to be the only party emitting these events, but the code that consumes the event never actually authenticates the sender — the same "trusted-caller assumption not enforced" pattern as the missing `owner` parameter that let `HolographedERC721` listeners react to events without being able to verify who triggered them. Here the effect is worse: it's not just an unverifiable parameter, it's a completely open write path into signer state.

### Impact Explanation
Any host that can reach the signer's event-listener port (default `0.0.0.0:30000` per the sample configs) can POST a forged `StackerDBChunksEvent`, `BlockValidateResponse`, `BurnBlockEvent`, or `StacksBlockEvent` body. These are fed directly into `SignerRunLoop::run_one_pass`, which drives the signer's block-validation/voting state machine. While the *signer message payloads themselves* (the `SignerMessage`s embedded in stackerdb chunks) still require a valid secp256k1 signature to recover a `pk` via `chunk.recover_pk()` [4](#0-3) , other event types (`BlockValidationResponse`, `NewBurnBlock`, `NewBlock`, and the `StatusCheck`) have no such cryptographic gate at all before being handed to the signer's decision logic, and the endpoint accepts connections from anyone, not just `127.0.0.1`/the paired node. This lets a remote, unauthenticated attacker inject spoofed node state (fake burn blocks, fake block-validation results) into a live signer process, which can influence signer voting/timing decisions — an unauthorized write to signer state reachable with a handful of HTTP requests.

### Likelihood Explanation
The listener is explicitly meant to be bound to an address reachable by the paired `stacks-node` (`endpoint = "0.0.0.0:30000"` is the sample default), and there is no code path enforcing that only the paired node can reach it. Any operator relying on `auth_token`/`auth_password` per the documentation would incorrectly assume this pairing is enforced end-to-end, when in fact it is not checked by the receiver at all. Reachability only requires network access to the configured port — no secrets, keys, or privileged roles are needed.

### Recommendation
Add an authentication check to `SignerEventReceiver::next_event()` (and `process_event`) that validates a shared secret/HMAC or bearer token on each inbound request before it is deserialized and forwarded, mirroring the intent already documented for `auth_token`/`auth_password`. At minimum, reject requests lacking a matching `Authorization` header, and/or bind the listener to loopback-only by default with an explicit opt-in for exposing it more broadly.

### Proof of Concept
1. Start a `stacks-signer` process with its event receiver bound to `0.0.0.0:30000` (per the sample `mainnet-signer-conf.toml`).
2. From a separate, unauthenticated host, send:
   ```
   POST /new_burn_block HTTP/1.1
   Host: <signer-ip>:30000
   Content-Type: application/json
   Content-Length: <n>

   {"burn_block_hash":"...","burn_block_height":999999,"consensus_hash":"...","parent_burn_block_hash":"..."}
   ```
3. Observe that `SignerEventReceiver::next_event()` accepts and parses this into `SignerEvent::NewBurnBlock` with no authentication check (`libsigner/src/events.rs:404-458`), and forwards it into the signer runloop via `forward_event()`, exactly as if it came from the legitimate paired node.

Note: I was not able to find any additional authentication middleware wrapping `HttpServer`/`tiny_http` elsewhere in the indexed portion of the repo; if such a check exists outside the indexed files, this finding should be revisited, but no such code appeared in `libsigner/**`, `stacks-signer/**`, or the node-side dispatcher that would gate the *inbound* side of this channel.

### Citations

**File:** libsigner/src/events.rs (L404-458)
```rust
    fn bind(&mut self, listener: SocketAddr) -> Result<SocketAddr, EventError> {
        self.http_server = Some(HttpServer::http(listener).expect("failed to start HttpServer"));
        self.local_addr = Some(listener);
        Ok(listener)
    }

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

**File:** libsigner/src/events.rs (L596-603)
```rust
                    let Ok(pk) = chunk.recover_pk() else {
                        warn!(
                            "Skipping signer chunk: signature recovery failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
```

**File:** docs/signing.md (L53-58)
```markdown
These settings **must** match between the node and signer configs:

| Signer Config   | Node Config                       | Must Match                    |
| --------------- | --------------------------------- | ----------------------------- |
| `auth_password` | `[connection_options] auth_token` | Exact string match            |
| `endpoint`      | `[[events_observer]] endpoint`    | Same host:port                |
```
