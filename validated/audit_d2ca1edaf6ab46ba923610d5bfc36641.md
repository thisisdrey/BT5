### Title
Signer event listener accepts unauthenticated `StackerDBChunksEvent` POSTs and forwards miner messages without any signature check - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver::next_event()` runs a plain HTTP server (`tiny_http`) that accepts `POST /stackerdb_chunks` requests and deserializes the body directly into a `StackerDBChunksEvent`, with no authentication, source-check, or shared secret. [1](#0-0)  For the miner-message lane, the resulting chunks are handed to `T::consensus_deserialize` and turned into `SignerEvent::MinerMessages` with **no signature recovery or verification at all**, unlike the sibling signer-message branch which at least calls `chunk.recover_pk()`. [2](#0-1) 

### Finding Description
The design intent is that this HTTP listener only receives events forwarded by the local Stacks node's event-observer plugin, after the node has already validated StackerDB chunk signatures via `StackerDBs::try_replace_chunk` / `PeerNetwork::validate_received_chunk`, which check `slot_desc.verify(&signer)` before ever accepting a chunk into local storage. [3](#0-2) [4](#0-3) 

However, the trust boundary breaks at the signer's own event ingestion endpoint: `process_event` simply reads the raw HTTP body and does `serde_json::from_slice(body.as_bytes())` into a `StackerDBChunksEvent`, with zero verification that the request actually originated from the paired local node, and zero re-validation of the chunk data or its embedded signature. [5](#0-4)  The dispatcher table itself performs no auth check — it routes purely on URL path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/shutdown`, `/new_block`). [6](#0-5) 

Crucially, when the contract is the miner-messages boot contract (`MINERS_NAME`), the code path converts each `chunk.data` directly via `consensus_deserialize` into `SignerEvent::MinerMessages` — it never calls `chunk.verify()`/`chunk.recover_pk()` against the expected slot signer, unlike the parallel signer-message branch a few lines below it which at least attempts `chunk.recover_pk()`. [7](#0-6)  Note even the signer-message branch's `recover_pk()` only checks that *a* signature is cryptographically recoverable — it does not check the recovered key hash against the actual expected slot signer for that reward-cycle contract; that authorization mapping exists only inside the node's own `StackerDBs` (which this endpoint bypasses entirely). [8](#0-7) 

This is a direct analog of the reported bug class: an auth-gate (chunk authenticity) that is enforced at one layer (the node's StackerDB write path) but silently fails open at a second, remote-reachable layer (the signer event listener), letting forged/unauthenticated data reach downstream logic as if it had already passed validation.

### Impact Explanation
If the signer's event-receiver bind address (`bind_addr` passed to `Signer::spawn`/`SignerEventReceiver::bind`) is reachable from the network — which is a plausible misconfiguration since nothing in this code enforces a loopback-only bind or authenticates the caller — a remote, unprivileged attacker can POST a crafted `StackerDBChunksEvent` JSON body directly to `/stackerdb_chunks` naming the miners' boot contract. Because the miner-message branch performs no signature check, arbitrary attacker-controlled `MinerMessages` are forwarded straight into the signer's processing pipeline as if the network/node had validated them. This is a network-wide propagation of forged data into consensus-adjacent tooling (signer decision inputs), matching the Critical impact category ("unauthenticated/unauthorized write to state..., network-wide propagation of forged data").

### Likelihood Explanation
Likelihood depends entirely on deployment configuration (whether the signer's HTTP event port is bound to a non-loopback interface / exposed through a reverse proxy or container port mapping) — a configuration mistake that is common in containerized/cloud deployments, and there is no code-level safeguard (auth header, mTLS, unix socket, or IP allowlist) preventing it even when misconfigured. Given the code offers no defense-in-depth at all for this endpoint, any exposure at the network layer is immediately and trivially exploitable with a single POST request.

### Recommendation
Add an authentication/authorization layer to the event receiver (e.g., a shared secret/HMAC header checked by `process_event`/`next_event`, or restrict to a Unix domain socket / mutually authenticated TLS), and independent of transport hardening, re-verify each chunk's signature against the expected StackerDB signer set (mirroring `StackerDBChunkData::verify`) before constructing `SignerEvent::MinerMessages`, rather than trusting that the upstream node has already done so.

### Proof of Concept
1. Start a signer with `SignerEventReceiver::bind` on an address reachable from the attacker (e.g. `0.0.0.0:<port>` instead of `127.0.0.1`).
2. From a remote unauthenticated host, send:
```
POST /stackerdb_chunks HTTP/1.1
Content-Type: application/json
Content-Length: <n>

{"contract_id": "<miners boot contract id>", "modified_slots": [{"slot_id":0,"slot_version":1,"sig":"00...","data":"<hex-encoded forged StacksMessageCodec-encoded miner message>"}]}
```
3. `next_event()` routes this to `process_event::<T, StackerDBChunksEvent>`, which deserializes it with no signature check for the miner-message contract branch, per the code at `libsigner/src/events.rs:544-567`. [7](#0-6) 
4. The forged message is delivered to the signer's `SignerRunLoop` as `SignerEvent::MinerMessages`, indistinguishable from a genuine, network-validated chunk.

### Citations

**File:** libsigner/src/events.rs (L413-459)
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

**File:** libsigner/src/events.rs (L544-567)
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

**File:** stackslib/src/net/stackerdb/mod.rs (L679-697)
```rust
        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }
```
