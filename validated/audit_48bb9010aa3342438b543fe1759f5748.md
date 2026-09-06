### Title
Signer's HTTP event listener accepts unauthenticated POST requests, allowing forged block-validation and burn-block events to be injected - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver::next_event` in `libsigner/src/events.rs` runs an HTTP server that the `stacks-node` is supposed to be the only client of, delivering `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, and `/new_block` events. Unlike StackerDB chunk delivery (which carries an inner secp256k1 signature that is checked via `chunk.recover_pk()`/`StackerDBChunkData::verify`), the `/proposal_response` (`BlockValidateResponse`) and `/new_burn_block`/`/new_block` event types carry **no signature or origin check at all**, and the HTTP handler performs no authentication (no `Authorization` header check, no source-IP restriction) before calling `process_event` and forwarding the parsed event into the signer's run loop.

### Finding Description
`next_event()` [1](#0-0)  dispatches incoming HTTP requests purely based on URL path, with zero authentication:
- `/status` → returns OK, no auth
- `/stackerdb_chunks` → `process_event::<T, StackerDBChunksEvent>`
- `/proposal_response` → `process_event::<T, BlockValidateResponse>`
- `/new_burn_block` → `process_event::<T, BurnBlockEvent>`
- `/new_block` → `process_event::<T, StacksBlockEvent>`

`process_event` [2](#0-1)  simply deserializes the JSON body and converts it into a `SignerEvent`, with no check that the request originated from the paired `stacks-node`. Contrast this with StackerDB chunk conversion, where each chunk's authenticity is at least checked with `chunk.recover_pk()` before being treated as a signer message [3](#0-2) ; `BlockValidateResponse` and burn/stacks-block events have no such per-message signature and rely entirely on the transport being trusted.

The intended protection against this is the shared `auth_token`/`auth_password` used on the node's HTTP endpoints (`/v3/block_proposal`, `/v2/blocks?broadcast=1`), documented in `docs/signing.md` [4](#0-3)  and enforced server-side by `stackslib/src/net/api/postblock_proposal.rs` and `postblock_v3.rs`. That token gates requests going *to* the node. However, the signer's own listener — the direction the node pushes events *to* the signer — has no analogous check. The sample configs even show it bound broadly (`endpoint = "0.0.0.0:30000"`) [5](#0-4) , and `mainnet-signer.toml`'s matching `[[events_observer]] endpoint` is likewise unauthenticated on the wire between node and signer [6](#0-5) .

This breaks the "authenticated vs. stored/consumed" equality: the signer run loop consumes `SignerEvent::BlockResponse`/`SignerEvent::NewBurnBlock` values as if they came from its paired node, but the transport does not verify the sender's identity for these event types.

### Impact Explanation
An attacker with network access to the signer's listening endpoint (which per sample configs may be bound to `0.0.0.0`) can:
- Forge `BlockValidateResponse` events to the signer, potentially causing the signer's `v0/signer.rs` block-response handling to act on results the real node never produced.
- Forge `BurnBlockEvent`/`StacksBlockEvent` events, feeding the signer a false view of the Bitcoin/Stacks chain state it uses to drive reward-cycle refresh and tip determination (`refresh_runloop`, `get_canonical_tip`).

This matches the report's bug class (missing authorization/permission check allowing an unprivileged party to influence privileged state) applied to the signer's inbound transport, and falls in the "steering a node off the tip via false inventory" / unauthorized write to state category from the rules.

### Likelihood Explanation
Exploitability depends on network exposure of the signer's event-listener port. Given official sample configs bind it to `0.0.0.0` and the only intended protection (`auth_token`) is documented for the node-facing endpoints, not this listener, a misconfigured or default-following deployment (signer endpoint reachable from outside localhost) is plausible. It requires no credentials, no key material, and no privileged role — purely network reachability to the signer's TCP port, satisfying the "remote, unprivileged" requirement.

### Recommendation
Require the same `auth_token`/`auth_password` (or an equivalent shared secret / mTLS) to be checked on all `SignerEventReceiver` HTTP routes (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`), not only on the node-facing RPC endpoints. Reject any request missing or presenting a mismatched `Authorization` header before calling `process_event`, mirroring the checks already implemented in `stackslib/src/net/api/postblock_proposal.rs` and `postblock_v3.rs`. Where possible, also document/enforce binding the signer's listener to localhost by default.

### Proof of Concept
1. Deploy a signer with `endpoint = "0.0.0.0:30000"` per the sample config.
2. From a separate machine with network access to port 30000, issue:
   ```
   POST /new_burn_block HTTP/1.1
   Host: <signer-ip>:30000
   Content-Type: application/json
   Content-Length: <n>

   {"burn_block_hash":"...","burn_block_height":999999,...}
   ```
3. The `SignerEventReceiver` accepts the request without any authentication check (see `next_event`/`process_event`), deserializes it as a `BurnBlockEvent`, and forwards it into the signer run loop as a legitimate `SignerEvent::NewBurnBlock`, with no way for the signer to detect it did not originate from its paired `stacks-node`.

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

**File:** libsigner/src/events.rs (L596-613)
```rust
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
```

**File:** docs/signing.md (L42-49)
```markdown
```toml
stacks_private_key = "<YOUR_SIGNER_PRIVATE_KEY_HEX>"
node_host = "127.0.0.1:20443"
endpoint = "0.0.0.0:30000"
network = "mainnet"
auth_password = "your-secret-token"
db_path = "/var/lib/stacks-signer/signerdb.sqlite"
```
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** sample/conf/mainnet-signer.toml (L24-28)
```text
# Signer event observer (REQUIRED).
# WARNING: endpoint must match your signer binary's `endpoint` config.
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```
