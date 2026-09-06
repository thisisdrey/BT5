### Title
Signer Event Receiver Accepts Unauthenticated POSTs Despite Documented `auth_token` Requirement - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver` — the HTTP listener that a `stacks-signer` process runs to receive node-pushed events (StackerDB chunks, block-validation responses, burn-block events, new-block events) — never validates any authentication credential on incoming requests, even though the project's own documentation and sample configs describe an `auth_token`/`auth_password` pair that is supposed to gate this exact channel.

### Finding Description
`EventReceiver::next_event()` for `SignerEventReceiver` dispatches purely on URL path with no credential check at all: [1](#0-0) 

`process_event()`, which is invoked for `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block`, reads the raw JSON body and converts it directly into a `SignerEvent`, again with no authentication step: [2](#0-1) 

Nowhere in `libsigner/src/events.rs`, `libsigner/src/runloop.rs`, or the rest of `libsigner/src/*` is there any `Authorization` header check, token comparison, or HMAC verification tied to the `auth_token`/`auth_password` setting. A repo-wide search of `libsigner/**` for `auth_token`, `Authorization`, and `auth_password` returned no matches — the value exists only in operator documentation and sample TOML files: [3](#0-2) [4](#0-3) 

The sample miner/node config for the *sending* side even documents an `auth_token` under `[connection_options]` that is meant to be matched against the signer's `auth_password`, implying the intended design was mutual authentication of this channel: [5](#0-4) 

But the signer-side transport (`libsigner`) that actually accepts the HTTP connection performs no such check. This breaks the equality the documentation promises: "authenticated node ↔ signer event feed" vs. what the code actually enforces, which is "any TCP client that can reach the listener." Sample configs explicitly bind this listener to all interfaces (`endpoint = "0.0.0.0:30000"`), so this is not merely a localhost-only surface: [6](#0-5) 

Downstream, some event types get partial cryptographic vetting later (e.g. StackerDB chunks are checked via `chunk.recover_pk()` / lane matching once turned into `SignerMessage`s): [7](#0-6) 
but `BurnBlockEvent` and `BlockValidateResponse` (the `/new_burn_block` and `/proposal_response` paths) carry no such signature check at the transport/event layer — the signer runloop consumes whatever is posted to those endpoints as if it originated from its configured node.

### Impact Explanation
Any network-reachable, unprivileged client (no node key, no signer key, no stacking-set membership required) can directly POST to a signer's `/proposal_response`, `/new_burn_block`, `/new_block`, or `/stackerdb_chunks` endpoints and have the payload accepted into the signer's internal event channel as though it came from the trusted node. This is a textbook "auth-gate that fails open": the feature (`auth_token`) is advertised and configured by operators, but the code path that should enforce it does not exist in `libsigner`. This falls squarely in the in-scope "libsigner transport files" category and constitutes an unauthenticated write into signer-facing state/queues — the kind of network-reachable access-control bypass analogous to the reported CVE class (improper access control allowing bypass of an intended security feature).

### Likelihood Explanation
High for reachability: the listener is a plain HTTP server (`tiny_http`) bound per operator config (frequently `0.0.0.0`) with no default deny/allowlist and no token check in the transport code, so exploitation only requires network reach to the port — no secrets, no privileged role, and no race condition.

### Recommendation
Implement and enforce the `auth_token`/`auth_password` check inside `SignerEventReceiver::next_event()`/`process_event()` (verify an `Authorization`/custom header against the configured secret before parsing/dispatching the body), and reject unauthenticated requests with 401 rather than silently `ack`-ing and processing them. If this is intentional (e.g., the check is meant to live in a layer not present in this snapshot), the discrepancy between documentation/sample-config promises and actual code should be resolved and confirmed.

### Proof of Concept
1. Stand up a `stacks-signer` with `endpoint = "0.0.0.0:30000"` per the sample config.
2. From a separate, unprivileged host with network access to port 30000, send:
   ```
   POST /new_burn_block HTTP/1.1
   Host: <signer-ip>:30000
   Content-Type: application/json
   Content-Length: <n>

   { ...forged BurnBlockEvent JSON... }
   ```
   with no `Authorization` header at all.
3. Observe (per `libsigner/src/events.rs:413-458`) that the request is accepted, deserialized via `process_event::<T, BurnBlockEvent>`, and forwarded into the signer runloop exactly as if it had been sent by the legitimately configured Stacks node — no comparison against the operator's configured `auth_token`/`auth_password` occurs anywhere in this call path.

Note: I was unable to find any authentication check anywhere else in the traced call graph (`libsigner/src/runloop.rs`, `libsigner/src/session.rs`, `libsigner/src/signer_set.rs` were not fully inspected line-by-line due to tool-call limits); if such a check exists in a file I did not fully review, this finding would need revision. Given the exhaustive search of `libsigner/**` for the relevant tokens returned zero hits, I am reasonably confident the gap is real, but flag this residual uncertainty explicitly.

### Citations

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

**File:** libsigner/src/events.rs (L544-614)
```rust
impl<T: SignerEventTrait> TryFrom<StackerDBChunksEvent> for SignerEvent<T> {
    type Error = EventError;

    fn try_from(event: StackerDBChunksEvent) -> Result<Self, Self::Error> {
        let received_time = SystemTime::now();
        let signer_event = if event.contract_id.name.as_str() == MINERS_NAME
            && event.contract_id.is_boot()
        {
            let mut messages = vec![];
            for chunk in event.modified_slots {
                match T::consensus_deserialize(&mut chunk.data.as_slice()) {
                    Ok(msg) => messages.push(msg),
                    Err(e) => {
                        debug!(
                            "Signer failed to deserialize miner chunk";
                            "slot_id" => chunk.slot_id,
                            "slot_version" => chunk.slot_version,
                            "data_len" => chunk.data.len(),
                            "error" => %e,
                        );
                    }
                }
            }
            SignerEvent::MinerMessages(messages)
        } else if event.contract_id.name.starts_with(SIGNERS_NAME) && event.contract_id.is_boot() {
            let Some((signer_set, message_id)) =
                get_signers_db_signer_set_message_id(event.contract_id.name.as_str())
            else {
                return Err(EventError::UnrecognizedStackerDBContract(event.contract_id));
            };
            // signer-XXX-YYY boot contract
            //
            // NOTE: the payload-type check below uses v0 `SignerMessageTypePrefix` semantics
            // (the mapping in `signer_message_payload_matches_lane` is fixed to v0). Future
            // signer-message versions must extend that mapping, or their chunks will not be
            // recognized here regardless of which `T` is in scope.
            let messages: Vec<_> = event
                .modified_slots
                .iter()
                .filter_map(|chunk| {
                    // Accept only payloads whose type is valid for this contract's message id.
                    let &type_byte = chunk.data.first()?;
                    let payload_kind = SignerMessageTypePrefix::from_u8(type_byte)?;
                    if !signer_message_payload_matches_lane(payload_kind, message_id) {
                        warn!(
                            "Skipping signer chunk with unexpected payload type for contract";
                            "contract" => %event.contract_id,
                            "lane_message_id" => message_id,
                            "payload_type_prefix" => type_byte,
                        );
                        return None;
                    }
                    let Ok(pk) = chunk.recover_pk() else {
                        warn!(
                            "Skipping signer chunk: signature recovery failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    let Ok(message) = read_next::<T, _>(&mut &chunk.data[..]) else {
                        warn!(
                            "Skipping signer chunk: payload deserialization failed";
                            "contract" => %event.contract_id,
                            "slot_id" => chunk.slot_id,
                        );
                        return None;
                    };
                    Some((chunk.slot_id, pk, message))
                })
                .collect();
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

**File:** sample/conf/testnet-signer.toml (L45-47)
```text
[connection_options]
# WARNING: Must match the signer binary's `auth_password`.
auth_token = ""
```

**File:** sample/conf/testnet-miner-conf.toml (L73-78)
```text
# ============================================================
# [connection_options] - Authentication for signer communication
# ============================================================
[connection_options]
# WARNING: Must match the signer's auth_password.
auth_token = "<YOUR_AUTH_TOKEN>"
```

**File:** sample/conf/testnet-miner-conf.toml (L84-87)
```text
# WARNING: endpoint must match your signer's endpoint config.
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```
