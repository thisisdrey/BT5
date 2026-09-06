### Title
Unauthenticated `SignerEventReceiver` HTTP listener accepts forged `StackerDBChunksEvent` and injects unverified miner messages into the signer - (File: libsigner/src/events.rs)

### Summary
`SignerEventReceiver`, the HTTP server a `stacks-signer` process binds to receive events pushed by a `stacks-node`, performs no authentication of incoming requests and, for the miner-message lane, accepts `StackerDBChunksEvent` payloads without verifying any chunk signature before turning them into `SignerEvent::MinerMessages`. Any host that can reach the signer's listening port can therefore inject forged miner messages directly into the signer's processing pipeline, completely bypassing the StackerDB write-path signature checks that normally gate what a real node would have accepted and re-broadcast.

### Finding Description
`SignerEventReceiver::bind()` opens a plain HTTP server with no credential check: [1](#0-0) 

`next_event()` dispatches on URL path alone — no header/token/IP check is performed before processing `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` POSTs: [2](#0-1) 

Compare this to the RPC surface in `stackslib`, where every sensitive POST endpoint (`/v3/block_proposal`, `/v3/blocks/*`, etc.) explicitly requires an `authorization` header matched against a configured secret, e.g.: [3](#0-2) 
The signer's own event-intake channel has no equivalent gate at all — this is the auth-gate-fails-open condition.

Once a POST body reaches `process_event`, it is parsed straight into a `StackerDBChunksEvent` and handed to `TryFrom<StackerDBChunksEvent> for SignerEvent<T>`: [4](#0-3) 

Inside that conversion, the miner-contract lane (`MINERS_NAME`) deserializes each chunk's raw bytes directly into a message with **no signature check at all**: [5](#0-4) 

This is asymmetric with the signer-contract lane immediately below it, which does call `chunk.recover_pk()` before accepting a message: [6](#0-5) 

Normally, a chunk only becomes visible to a `stacks-node`'s StackerDB replica — and thus eligible to be turned into a `StackerDBChunksEvent` — after passing `StackerDBTx::try_replace_chunk`, which verifies the chunk's signature against the slot's registered owner address before storing it: [7](#0-6) 

Because the signer's HTTP listener has no authentication, an attacker does not need to go through this validated write path at all: they can craft an arbitrary `StackerDBChunksEvent` JSON body (with any `contract_id` set to the boot miners contract and arbitrary `modified_slots` data) and POST it directly to the signer's bound endpoint. For the `MINERS_NAME` lane, that forged payload is deserialized and forwarded as `SignerEvent::MinerMessages` with zero cryptographic verification — breaking the intended equality that "a message reaching the signer's event stream implies it was validated and stored by an authentic StackerDB replica."

The sample and reference configs commonly bind this listener on `0.0.0.0`, i.e., network-reachable rather than loopback-only: [8](#0-7) [9](#0-8) 

### Impact Explanation
Any unauthenticated, unprivileged party who can reach the `stacks-signer`'s event-receiver port can inject forged `SignerEvent::MinerMessages` (and other event types) into the signer's runloop without possessing any node/signer secret, any StackerDB slot-owner private key, or exploiting a race — because the miner-message lane performs no signature validation whatsoever and the HTTP listener has no auth gate. This is an unauthenticated write of forged/unvalidated data into signer-facing state, which is explicitly listed as a Critical-severity outcome in the validation criteria. Depending on how `SignerEvent::MinerMessages` are consumed downstream in the signer's coordination logic, this could influence miner-message-driven decisions using data that never passed through any of the node's chain- or StackerDB-level authentication.

### Likelihood Explanation
The attack requires only network reachability to the signer's bound HTTP port (frequently configured as `0.0.0.0:<port>` per the sample configs) and knowledge of the well-known handler paths (`/stackerdb_chunks`, etc.), which are publicly documented in this same file. No cryptographic material, no valid StackerDB signer key, and no interaction with the stacks-node's own RPC auth token is needed, since the signer's event-intake endpoint checks none of that.

### Recommendation
Add authentication to the signer's event-receiver HTTP server (e.g., require and check a shared secret/HMAC header on every incoming POST, mirroring the `auth_token` mechanism already used for `/v3/block_proposal`), and additionally verify chunk signatures against the expected StackerDB slot owners for the `MINERS_NAME` lane before constructing `SignerEvent::MinerMessages`, consistent with the verification already performed on the `SIGNERS_NAME` lane.

### Proof of Concept
1. Start a `stacks-signer` bound per the sample config (`endpoint = "0.0.0.0:30000"`), reachable from an attacker-controlled host.
2. From any host on the network (no credentials needed), send:
```
POST /stackerdb_chunks HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{"contract_id": {"issuer": [...boot address...], "name": "miners"}, "modified_slots": [{"slot_id":0,"slot_version":1,"sig":"<any 65-byte hex sig>","data":"<hex-encoded forged SignerMessage>"}]}
```
3. `SignerEventReceiver::next_event` (`libsigner/src/events.rs:437-438`) routes this to `process_event::<T, StackerDBChunksEvent>`, which JSON-deserializes it and calls `TryFrom<StackerDBChunksEvent>`.
4. Because `contract_id.name == MINERS_NAME`, the code at `libsigner/src/events.rs:552-567` deserializes `chunk.data` directly into a message and emits `SignerEvent::MinerMessages([...])` without ever checking `sig` — the forged event is delivered to the signer's runloop exactly as if it had been legitimately relayed by a real, authenticated stacks-node.

**Note on completeness:** I was unable to fully inspect `stacks-node/src/event_dispatcher.rs` (only match counts were returned, not content) to confirm whether the node-side sender attaches any authentication header when it normally POSTs to this endpoint. However, this does not affect the finding: `SignerEventReceiver::next_event` in `libsigner/src/events.rs` performs no server-side check of any such header regardless of what the legitimate sender does, so the endpoint is fail-open by construction.

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

**File:** libsigner/src/events.rs (L547-567)
```rust
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
```

**File:** libsigner/src/events.rs (L568-619)
```rust
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
            SignerEvent::SignerMessages {
                signer_set,
                messages,
                received_time,
            }
```

**File:** stackslib/src/net/api/postblock_proposal.rs (L1135-1144)
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

**File:** stackslib/src/net/stackerdb/db.rs (L400-423)
```rust
    pub fn try_replace_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        // Check per-replica chunk-size cap.
        if (chunk.len() as u64) > self.config.chunk_size {
            return Err(net_error::StackerDBChunkTooBig(chunk.len()));
        }

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

**File:** sample/conf/testnet-signer.toml (L33-37)
```text
# Signer event observer (REQUIRED).
# WARNING: endpoint must match your signer binary's `endpoint` config.
[[events_observer]]
endpoint = "127.0.0.1:30000"
events_keys = ["stackerdb", "block_proposal", "burn_blocks"]
```
