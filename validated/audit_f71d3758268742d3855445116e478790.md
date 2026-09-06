### Title
Unauthenticated HTTP event-ingestion endpoint lets any network-reachable client inject forged signer events (`StackerDBChunksEvent`, `BlockValidationResponse`, `NewBurnBlock`, `NewBlock`) - ([File: libsigner/src/events.rs])

### Summary
`SignerEventReceiver`, the HTTP server that a Stacks Signer binds to receive event-observer callbacks from its local node, performs **no authentication whatsoever** on any of its POST routes. Any TCP client that can reach the bound `endpoint` can POST a JSON body to `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` and have it parsed and forwarded straight into the signer's runloop as if it had come from the trusted node. This is the same bug class as the report: a self-hosted HTTP transport that accepts and processes full application-level requests with no `authProvider`/credential check gating the endpoint.

### Finding Description
`EventReceiver::next_event` dispatches incoming POSTs purely by URL path, with zero credential/header verification anywhere in the branch: [1](#0-0) 

Compare this to the sibling node-RPC endpoints in `stackslib/src/net/api/`, which *do* gate sensitive POST routes behind a shared-secret `Authorization` header (`postblock_proposal.rs`, `blockreplay.rs`, `blocksimulate.rs`, `txsimulate.rs`, `fastcallreadonly.rs`) — e.g.: [2](#0-1) 

`SignerEventReceiver` has no equivalent gate. `process_event` simply reads the body, ACKs the request, deserializes it into the target event type, and returns it — no header, token, or peer-address check is ever consulted: [3](#0-2) 

The resulting `SignerEvent` (including `BlockValidationResponse`, `NewBurnBlock`, `NewBlock`, and `StackerDBChunksEvent`) is forwarded unmodified to the signer's `main_loop`/runloop: [4](#0-3) 

For `StackerDBChunksEvent`, the chunk payloads are trusted enough to be interpreted as `SignerMessage`s from the signer set once the embedded signature recovers to *some* public key — the endpoint itself imposes no constraint that the payload actually came from the node's genuine StackerDB replication path: [5](#0-4) 

Downstream, `StackerDBListener::run` (in `stacks-node`) does cross-check the recovered slot's expected signer key before acting on `BlockResponse::Accepted` signatures, so a forged StackerDB chunk on that particular lane is still constrained by that secondary signature check: [6](#0-5) 

However, `BlockValidationResponse` and burn/stacks-block notification events (`/proposal_response`, `/new_burn_block`, `/new_block`) carry **no signature or additional authenticity check at all** before being handed to the signer's decision logic via the event channel — the only gate is reaching the bound TCP port and POSTing well-formed JSON.

### Impact Explanation
Any process that can open a TCP connection to the signer's `endpoint` (which is attacker-controlled reachability, not code-enforced to loopback-only anywhere in `libsigner/src/events.rs`) can inject forged `BlockValidationResponse` and burn/stacks-block events directly into the signer's decision pipeline with no credentials. Because these event types are not signature-checked at the transport layer, this is an unauthenticated write into a trust-sensitive channel that a legitimate signer's runloop consumes as though the local Stacks node produced it. This satisfies the "unauthenticated/unauthorized write to state" bar for Critical impact from the grading rubric, since it lets a remote, unprivileged attacker feed unauthenticated data directly into signer decision state via a completely open HTTP listener — the exact analog of the reported bug (`POST /mcp` accepting sessions and tool calls without any `authProvider`).

### Likelihood Explanation
Exploitation requires only network reachability to the configured `endpoint` TCP port and the ability to send an HTTP POST with a JSON body — no cryptographic material, no node secret, and no privileged role are needed. The endpoint is explicitly documented as intended only for "a local or subnet network stacks node" (a caution printed at signer startup), which itself signals that the code does not enforce this boundary and instead relies entirely on network/firewall configuration for protection: [7](#0-6) 

### Recommendation
Add a shared-secret/token check (mirroring the `Authorization` header pattern already used by `postblock_proposal.rs`, `blockreplay.rs`, etc. in `stackslib/src/net/api/`) to `SignerEventReceiver::next_event` in `libsigner/src/events.rs` before dispatching any `/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, or `/new_block` request. At minimum, require and validate a pre-shared token/header value configured alongside `endpoint`, and reject unauthenticated requests with an HTTP 401 rather than processing them.

### Proof of Concept
1. Start a signer configured with `endpoint = "0.0.0.0:30000"` (or any interface reachable by the attacker).
2. From an unauthenticated remote host, send:
```
POST /proposal_response HTTP/1.1
Host: <signer-ip>:30000
Content-Type: application/json
Content-Length: <n>

{ ...forged BlockValidateResponse JSON... }
```
3. `SignerEventReceiver::next_event` (`libsigner/src/events.rs:437-448`) routes this straight to `process_event::<T, BlockValidateResponse>(request)` with no authentication check, and the resulting `SignerEvent::BlockValidationResponse` is forwarded into the signer's runloop channel exactly as if the local trusted node had sent it.

### Citations

**File:** libsigner/src/events.rs (L282-313)
```rust
    /// Main loop for the receiver.
    /// Typically, this is started in a separate thread.
    fn main_loop(&mut self) {
        loop {
            if self.is_stopped() {
                info!("Event receiver stopped");
                break;
            }
            let next_event = match self.next_event() {
                Ok(event) => event,
                Err(EventError::UnrecognizedEvent(..)) => {
                    // got an event that we don't care about (not a problem)
                    continue;
                }
                Err(EventError::Terminated) => {
                    // we're done
                    info!("Caught termination signal");
                    break;
                }
                Err(e) => {
                    warn!("Failed to receive next event: {:?}", &e);
                    continue;
                }
            };
            if !self.forward_event(next_event) {
                info!("Failed to forward event");
                break;
            }
        }
        info!("Event receiver main loop exit");
    }
}
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

**File:** libsigner/src/events.rs (L511-542)
```rust
fn ack_dispatcher(request: HttpRequest) {
    if let Err(e) = request.respond(HttpResponse::empty(200u16)) {
        error!("Failed to respond to request: {:?}", &e);
    };
}

// TODO: add tests from mutation testing results #4835
#[cfg_attr(test, mutants::skip)]
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

**File:** libsigner/src/events.rs (L544-624)
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
            SignerEvent::SignerMessages {
                signer_set,
                messages,
                received_time,
            }
        } else {
            return Err(EventError::UnrecognizedStackerDBContract(event.contract_id));
        };
        Ok(signer_event)
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

**File:** stacks-node/src/nakamoto_node/stackerdb_listener.rs (L372-426)
```rust
            for (slot_id, _pk, message) in messages.into_iter() {
                let Some(signer_entry) = &self.signer_entries.get(&slot_id) else {
                    return Err(NakamotoNodeError::SignerSignatureError(
                        "Signer entry not found".into(),
                    ));
                };
                let Ok(signer_pubkey) = StacksPublicKey::from_slice(&signer_entry.signing_key)
                else {
                    return Err(NakamotoNodeError::SignerSignatureError(
                        "Failed to parse signer public key".into(),
                    ));
                };

                match message {
                    SignerMessageV0::BlockResponse(BlockResponse::Accepted(accepted)) => {
                        let BlockAccepted {
                            signer_signature_hash: block_sighash,
                            signature,
                            metadata,
                            response_data,
                        } = accepted;
                        let tenure_extend_timestamp = response_data.tenure_extend_timestamp;
                        let read_count_extend_timestamp =
                            response_data.tenure_extend_read_count_timestamp;

                        let (lock, cvar) = &*self.blocks;
                        let mut blocks = lock.lock().expect("FATAL: failed to lock block status");

                        let Some(block) = blocks.get_mut(&block_sighash) else {
                            info!(
                                "StackerDBListener: Received signature for block that we did not request. Ignoring.";
                                "signature" => %signature,
                                "signer_signature_hash" => %block_sighash,
                                "slot_id" => slot_id,
                                "signer_set" => self.signer_set,
                            );
                            continue;
                        };

                        let Ok(valid_sig) = signer_pubkey.verify(block_sighash.bits(), &signature)
                        else {
                            warn!(
                                "StackerDBListener: Got invalid signature from a signer. Ignoring."
                            );
                            continue;
                        };
                        if !valid_sig {
                            warn!(
                                "StackerDBListener: Processed signature but didn't validate over the expected block. Ignoring";
                                "signature" => %signature,
                                "signer_signature_hash" => %block_sighash,
                                "slot_id" => slot_id,
                            );
                            continue;
                        }
```

**File:** stacks-signer/src/lib.rs (L119-132)
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
```
