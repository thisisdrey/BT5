### Title
`SignerEventReceiver` accepts unauthenticated `/stackerdb_chunks` events, letting the miner-message lane bypass all StackerDB signer/signature checks - (File: `libsigner/src/events.rs`)

### Summary
`SignerEventReceiver::next_event` (`libsigner/src/events.rs`) exposes a plain, unauthenticated `tiny_http` server that accepts POSTed `StackerDBChunksEvent` JSON on `/stackerdb_chunks`. This endpoint is meant to only ever be called by the local node's event-observer dispatcher after a chunk has already passed StackerDB slot-signature verification (`try_replace_chunk` / `validate_received_chunk`). But the HTTP receiver itself performs no authentication and, for the `MINERS_NAME` contract lane, performs no signature check at all on the chunk contents before converting it into a trusted `SignerEvent`.

### Finding Description
The intended trust chain is: a chunk is POSTed to `/v2/stackerdb/.../chunks`, validated against the slot's registered signer key in `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:398-439`) and `PeerNetwork::validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:649-718`), and only successfully-stored chunks are forwarded to the node's event dispatcher (`stacks-node/src/event_dispatcher.rs:883-925`), which POSTs them to the signer's `SignerEventReceiver`. [1](#0-0) [2](#0-1) 

However, the receiving side — `SignerEventReceiver::next_event` — treats any HTTP POST to `/stackerdb_chunks` as authoritative, with no shared secret, token, or peer check: [3](#0-2) 

Once received, `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` (`libsigner/src/events.rs:544-625`) branches on the contract name embedded in the event payload (fully attacker-controlled, since the whole `StackerDBChunksEvent` including `contract_id` is deserialized from the POST body). For the `MINERS_NAME` boot-contract lane, chunk data is deserialized directly into a `SignerEvent::MinerMessages` **with no signature or slot-ownership check whatsoever**: [4](#0-3) 

For the `SIGNERS_NAME` lane, the code does call `chunk.recover_pk()`, but this only cryptographically recovers *some* public key from the attached signature bytes — it never checks that the recovered key equals the actual signer assigned to that `slot_id` for the current reward cycle. Any self-signed, well-formed signature passes this stage: [5](#0-4) 

This mirrors the report's bug class exactly: `SwapperCallbackValidation.verifyCallback()` checked "is this call coming from *a* legitimate Swapper" rather than "is this the *specific* Swapper this contract is expecting," letting an attacker deploy their own Swapper and call the sensitive function directly, bypassing the intended validation path entirely (`execCalls` on `WalletImpl`). Here, the "expected caller" is the local Stacks node relaying already-verified StackerDB chunks; the actual guard is "is this JSON well-formed and does it deserialize," not "did this data actually come through StackerDB chunk-signature validation, and is the recovered key the one authorized for this slot/reward-cycle." Any process that can reach the bound TCP port can POST directly, replicating exactly the "call the internal function directly, skip the real check" pattern.

### Impact Explanation
If a signer node's event-receiver port is reachable by an unprivileged network peer (e.g., misconfigured to bind non-loopback, or reachable via a container/host network path), an attacker can inject forged `StackerDBChunksEvent` payloads that:
- Feed the `.miners` lane (`SignerEvent::MinerMessages`) with fabricated miner messages, with zero authentication or signature check, directly influencing signer/miner coordination logic that consumes these events.
- Feed the `.signers-*` lane with messages whose "signer" `pk` is self-generated and never checked against the actual slot's registered signer — the signature-recovery step provides no real authorization, only a syntactic sanity check.

This corresponds to "unauthenticated/unauthorized write to state" and "network-wide propagation of forged data" in the rubric, since these events feed the signer's runloop / `StackerDBListener` state (block-signature tallying, idle timestamps, etc.) without ever having passed through the actual StackerDB replica's signer-authenticity gate.

### Likelihood Explanation
Reachability depends on the signer's/node's configured event-observer endpoint binding. In default, well-isolated (localhost-only) deployments this is not remotely reachable, which is a real mitigating factor. But nothing in `SignerEventReceiver` itself enforces this — there is no authentication mechanism at the protocol layer, so the security boundary is entirely deployment-configuration dependent, exactly the situation the report's `verifyCallback()` bug represents (a validation that looks like a real gate but isn't). Given the endpoint binds and listens per configured `SocketAddr` (`libsigner/src/events.rs:404-408`), any operator who exposes it (intentionally or accidentally, e.g., via port-forwarding, cloud misconfig, or shared infra) is fully exposed.

### Recommendation
- Add authentication to the `SignerEventReceiver` HTTP server (e.g., a shared secret/bearer token configured between the node's event observer and the signer, checked on every request) rather than relying solely on network isolation.
- For the `SIGNERS_NAME` lane, verify that `chunk.recover_pk()` matches the actual registered signer for `chunk.slot_id` in the current reward cycle before trusting the message, rather than passing along an unverified recovered key.
- For the `MINERS_NAME` lane, likewise verify the chunk's signature against a known/expected miner key set before deserializing it into a trusted event.

### Proof of Concept
1. Identify or gain reachability to a signer's configured event-receiver socket address (the `endpoint` passed to `SpawnedSigner::new`, bound in `SignerEventReceiver::bind`).
2. Craft a `StackerDBChunksEvent` JSON body with `contract_id` set to the local `.miners` boot contract identifier and `modified_slots` containing any chunk whose `data` deserializes as a valid `SignerMessageV0`/`T`; the `sig` field on the chunk is irrelevant since the `MINERS_NAME` branch never checks it.
3. POST this JSON to `http://<signer-endpoint>/stackerdb_chunks` with `Content-Type: application/json`.
4. Observe that `SignerEventReceiver::next_event` accepts the request (per `libsigner/src/events.rs:437-438`, `process_event::<T, StackerDBChunksEvent>(request)`), and `TryFrom` converts it directly into `SignerEvent::MinerMessages` (`libsigner/src/events.rs:549-567`) without ever consulting the real StackerDB storage, slot signer, or chunk signature — bypassing the entire signature-based authorization chain that a genuine chunk would have had to pass through `try_replace_chunk`.

Note: I was not able to fully verify, within the available tooling, whether any production deployment path binds this receiver on a non-loopback interface by default, or whether an additional network-layer control (e.g., firewalling in `stacks-signer` config docs) is documented/enforced elsewhere outside the indexed files. That configuration-dependent exposure is the main uncertainty affecting exploitability in a default deployment.

### Citations

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

**File:** stacks-node/src/event_dispatcher.rs (L883-909)
```rust
    /// Forward newly-accepted StackerDB chunk metadata to downstream `stackerdb` observers.
    /// Infallible.
    pub fn process_new_stackerdb_chunks(
        &self,
        contract_id: QualifiedContractIdentifier,
        modified_slots: Vec<StackerDBChunkData>,
    ) {
        debug!(
            "event_dispatcher: New StackerDB chunk events for {contract_id}: {modified_slots:?}"
        );

        let interested_observers = self.filter_observers(&self.stackerdb_observers_lookup, false);

        let stackerdb_channel = self
            .stackerdb_channel
            .lock()
            .expect("FATAL: failed to lock StackerDB channel mutex");
        let interested_receiver = stackerdb_channel.is_active(&contract_id);
        if interested_observers.is_empty() && interested_receiver.is_none() {
            return;
        }

        let event = StackerDBChunksEvent {
            contract_id,
            modified_slots,
        };
        let payload = serde_json::to_value(&event)
```

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

**File:** libsigner/src/events.rs (L583-613)
```rust
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
```
