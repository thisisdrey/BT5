### Title
Unauthenticated signer event-listener trusts forged miner `StackerDBChunksEvent` without any signature check - ([File: libsigner/src/events.rs])

### Summary
The `SignerEventReceiver`'s HTTP endpoint (`/stackerdb_chunks`) that the node uses to push StackerDB events to a `stacks-signer` process performs no authentication of the caller and, for chunks on the miner's slot (`MINERS_NAME` contract), performs no signature verification at all before treating the payload as authoritative miner data. This mirrors the EdgeX advisory's bug class: an interface that is supposed to be gated (implicitly, by only accepting data from the trusted node) instead accepts and processes attacker-controlled input as if it came from the trusted party.

### Finding Description
`SignerEventReceiver::next_event` in [1](#0-0)  accepts any HTTP POST to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/status`, or `/shutdown` with **no authentication check whatsoever** — no token, no header, no peer-address restriction is verified anywhere in `bind()` or `next_event()` (see [2](#0-1) ).

When a `StackerDBChunksEvent` is posted for the miner's boot contract, `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` deserializes `chunk.data` directly into a `SignerMessage` with **no signature/owner check at all**: [3](#0-2) 
This is in stark contrast to the sibling branch for `.signers-*` contracts a few lines below, which calls `chunk.recover_pk()` and validates the payload type before accepting the message: [4](#0-3) .

Normally, chunk authenticity for the miner slot is expected to have already been enforced upstream, at the point the *node* accepted/stored the chunk via `StackerDBs::try_replace_chunk` (which does check `slot_desc.verify(&slot_validation.signer)`, see [5](#0-4) ). The event push from node → signer is supposed to be a "re-statement" of already-validated data over a private channel. But the `SignerEventReceiver` HTTP listener does not verify that the sender of this HTTP POST is actually the local, trusted node — it will accept and process a `StackerDBChunksEvent` JSON body from *any* TCP client that can reach the listening port, and for the miner lane it does not re-validate any signature on the chunk contents before turning it into a `SignerEvent::MinerMessages` that is fed into the signer's core `SignerRunLoop`.

The documented/sample configuration explicitly shows this endpoint bound to all interfaces: `endpoint = "0.0.0.0:30000"` in [6](#0-5) , and `docs/signing.md` corroborates the same non-loopback binding as an accepted configuration pattern ( [7](#0-6) ). Contrast this with the actual node-side RPC endpoints that talk to the signer, which *do* enforce an equality check on an authorization header (e.g. `/v3/block_proposal`, `/v3/blocks/simulate/...`, `/v3/contracts/fast-call-read/...`), all requiring `auth_header == password` before accepting a POST ( [8](#0-7) , [9](#0-8) , [10](#0-9) ). The signer's own inbound listener has no equivalent gate.

### Impact Explanation
If the signer's event-listener port is reachable by an unauthenticated network peer (which the shipped sample configuration explicitly enables via `0.0.0.0` binding), an attacker can:
- POST a forged `StackerDBChunksEvent` JSON body for the miner's boot contract directly to `/stackerdb_chunks`.
- Have it deserialized into `SignerEvent::MinerMessages` with zero signature verification, injecting attacker-controlled `SignerMessage` values (e.g. spoofed miner data) straight into the signer's `SignerRunLoop` state machine.
- This breaks the authenticated-vs-accepted equality that the rest of the system relies on (chunk signature ⇒ trusted origin), and can influence signer-side decision logic that is driven by "received miner data," steering the signer with data that never came from a legitimate miner or node.

This is a "forged-data-propagation into node/signer state" class finding — an auth gate that is effectively absent, not merely fail-open on a comparison. It could allow injection of forged data into the signer's operational pipeline.

### Likelihood Explanation
Exploitability is contingent on network exposure of the signer's event-listener port. The codebase's own sample configuration and documentation both illustrate binding the listener to `0.0.0.0` (all interfaces) rather than `127.0.0.1`, which is a realistic deployment scenario for signers hosted on separate infra from the node. Given that binding, exploitation requires only a single unauthenticated HTTP POST — no cryptographic material, no privileged access, and no traffic volume. The finding is best characterized as a design gap (missing authentication on an inbound push endpoint) rather than a definite exploitable defect in every deployment, since a loopback-only binding would mitigate it; I was not able to fully verify whether newer code or deployment guidance mandates loopback-only binding, or whether downstream signer logic (e.g., `stacks-signer/src/v0/signer.rs` miner-message handling) applies any additional validation that would neutralize forged `MinerMessages` before they have effect — this file's relevant handling logic was only partially inspected within tool-call limits.

### Recommendation
- Require the `SignerEventReceiver` HTTP listener to authenticate incoming requests (e.g., a shared secret/HMAC header, mirroring the `connection_options.auth_token` pattern already used for `/v3/block_proposal`), rather than relying solely on network topology.
- For the miner (`MINERS_NAME`) lane in `TryFrom<StackerDBChunksEvent> for SignerEvent<T>`, verify chunk signatures/owner (as is already done for the `.signers-*` lane via `chunk.recover_pk()`) before constructing `SignerEvent::MinerMessages`.
- Update sample configs and documentation to default/require loopback binding (`127.0.0.1`) for the signer's event listener unless a proper authentication mechanism is in place.

### Proof of Concept
1. Deploy a `stacks-signer` using `sample/conf/signer/mainnet-signer-conf.toml`'s example `endpoint = "0.0.0.0:30000"`.
2. From a separate, unauthenticated host, craft an HTTP POST to `http://<signer-ip>:30000/stackerdb_chunks` with a JSON body matching `StackerDBChunksEvent`, setting `contract_id` to the miner's boot contract (`MINERS_NAME`, `is_boot() == true`) and `modified_slots` containing arbitrary attacker-chosen bytes as `chunk.data` (signature field can be anything, since it's never checked on this lane).
3. Observe that `SignerEventReceiver::next_event` accepts the POST, and `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` deserializes the attacker payload into `SignerEvent::MinerMessages` without any signature check, forwarding it into the signer's runloop channel.

### Citations

**File:** libsigner/src/events.rs (L398-408)
```rust
impl<T: SignerEventTrait> EventReceiver<T> for SignerEventReceiver<T> {
    type ST = SignerStopSignaler;

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

**File:** libsigner/src/events.rs (L549-567)
```rust
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
```

**File:** libsigner/src/events.rs (L580-614)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L411-423)
```rust
        let slot_validation = self
            .get_slot_validation(smart_contract, slot_desc.slot_id)?
            .ok_or(net_error::NoSuchSlot(
                smart_contract.clone(),
                slot_desc.slot_id,
            ))?;

        if !slot_desc.verify(&slot_validation.signer)? {
            return Err(net_error::BadSlotSigner(
                slot_validation.signer,
                slot_desc.slot_id,
            ));
        }
```

**File:** sample/conf/signer/mainnet-signer-conf.toml (L37-39)
```text
# REQUIRED: Local endpoint this signer listens on for events from the node.
# Must match the endpoint in the node's [[events_observer]] section.
endpoint = "0.0.0.0:30000"
```

**File:** docs/signing.md (L37-49)
```markdown
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

**File:** stackslib/src/net/api/blocksimulate.rs (L152-161)
```rust
        // If no authorization is set, then the block replay endpoint is not enabled
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

**File:** stackslib/src/net/api/fastcallreadonly.rs (L101-110)
```rust
        // If no authorization is set, then the block proposal endpoint is not enabled
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
