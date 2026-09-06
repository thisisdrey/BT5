Confirmed: no source-IP allowlisting or peer-address check exists anywhere in `libsigner/src/events.rs`; the `SignerEventReceiver` accepts and processes any TCP connection that speaks HTTP to its bound port, distinguishing requests only by URL path. This confirms the finding below.

### Title
Unauthenticated event listener accepts forged, unsigned StackerDB miner-message events - (File: libsigner/src/events.rs)

### Summary
The signer's `SignerEventReceiver` HTTP listener, which is meant to only receive trusted `StackerDBChunksEvent` notifications pushed by the local, paired Stacks node, performs no authentication of the caller and, for `.miners` StackerDB events, never verifies the chunk's embedded signature before turning it into a `SignerEvent::MinerMessages` that is fed into the signer's runloop.

### Finding Description
`SignerEventReceiver::next_event` dispatches incoming HTTP POSTs purely by URL path (`/stackerdb_chunks`, `/proposal_response`, `/new_burn_block`, `/new_block`, `/shutdown`, `/status`) with no header, token, or peer-address check of any kind [1](#0-0) . `process_event` simply reads the body and JSON-deserializes it into the target event type, then converts it into a `SignerEvent` — again with no authentication of the sender [2](#0-1) .

The conversion `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` has two code paths depending on which boot contract the event claims to be for. For the `SIGNERS_NAME` (`.signer-N-M`) contracts, each chunk's signature is checked via `chunk.recover_pk()` before the message is accepted [3](#0-2) . But for the `MINERS_NAME` (`.miners`) contract, chunks are deserialized directly with no signature check at all: [4](#0-3) 

This breaks the equality that should hold between "chunk actually written by the slot's owner and served by the paired node" and "chunk data accepted by the signer as an authentic miner message." Because the HTTP listener itself has no authentication, and the `.miners` conversion path has no signature check either, an attacker able to reach the signer's event-receiver port can POST an arbitrary, unsigned `StackerDBChunksEvent` JSON body claiming to target the `.miners` contract, and it will be accepted and forwarded into the signer's runloop as genuine `SignerEvent::MinerMessages`, indistinguishable from data that arrived through the real, StackerDB-validated P2P path (where `validate_received_chunk` in `stackslib/src/net/stackerdb/mod.rs` does enforce slot-signer verification before a chunk is ever accepted into the DB) [5](#0-4) .

This is the closest in-scope analog to the reported CSRF class: the reported bug is about a server accepting a state-changing action without verifying the true origin/authorization of the request. Here, the signer's HTTP listener accepts a state-injecting action (forwarding a "miner message" event into the signer's trusted event stream) with no verification of who sent it and, for this one message class, no fallback cryptographic verification either.

### Impact Explanation
Any remote, unauthenticated party who can reach the signer's event-receiver TCP port can inject fabricated miner messages into the signer process, bypassing both transport-level authentication and the chunk-signature check that protects every other event type. This is an unauthenticated write into signer state (forged-data injection distinct from the properly-authenticated StackerDB P2P path), matching the "unauthenticated/unauthorized write to state" criterion. The downstream handling of `SignerEvent::MinerMessages` inside the signer runloop is signer decision logic and explicitly out of scope for further analysis here, but the injection point itself — accepting and forwarding unauthenticated, unverified data as if it were a genuine miner chunk — is squarely in the in-scope `libsigner` transport code.

### Likelihood Explanation
Exploitability depends entirely on network exposure of the signer's event-receiver bind address; if it is bound to a loopback-only interface it is not remotely reachable. However, nothing in `libsigner/src/events.rs` or `SignerEventReceiver::bind` enforces a loopback-only bind or checks the peer's address, so this is a configuration-dependent but code-level-absent protection: the code provides no defense-in-depth against a non-loopback deployment, and no signature check exists for `.miners` chunks even for well-intentioned but compromised/spoofable senders on the same network segment.

### Recommendation
- Add authentication (e.g., a shared secret/token, mTLS, or a signature over the event body) between the paired Stacks node and the signer's event receiver, similar to the `authorization` header pattern used elsewhere in the node's RPC API (`postblock_v3.rs`, `blocksimulate.rs`, `txsimulate.rs`).
- Restrict `SignerEventReceiver::bind` to loopback addresses by default, or explicitly document/require operators to firewall the port.
- Make the `.miners` branch of `TryFrom<StackerDBChunksEvent> for SignerEvent<T>` verify `chunk.recover_pk()`/`chunk.verify()` against the expected miner signer, consistent with the `.signer-N-M` branch, so that forged/garbage chunks cannot be turned into accepted `MinerMessages` even if delivered by the legitimate node forwarding an already-invalid chunk.

### Proof of Concept
1. Identify a reachable signer event-receiver endpoint (the address the signer binds via `SignerEventReceiver::bind`, e.g. `<signer-host>:<port>`).
2. Craft an HTTP POST to `/stackerdb_chunks` with a JSON body deserializable as `StackerDBChunksEvent`, whose `contract_id` is the `.miners` boot contract and whose `modified_slots` contain a `StackerDBChunkData` with attacker-chosen `data` and an empty/garbage `sig` field.
3. Send it directly via `TcpStream`, mirroring the test harness pattern already present in `libsigner/src/tests/mod.rs` (`mock_stacks_node` thread) but omitting the `chunk.sign(&privk)` step used there [6](#0-5) .
4. Observe that `process_event`/`TryFrom<StackerDBChunksEvent>` accepts the chunk into `SignerEvent::MinerMessages` without ever calling `chunk.recover_pk()`, unlike the equivalent `.signer-N-M` path, confirming the missing authentication/verification for this message class.

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

**File:** libsigner/src/tests/mod.rs (L103-146)
```rust
    for i in 0..max_events {
        let privk = Secp256k1PrivateKey::random();
        let message =
            SignerMessage::BlockPreCommit(Sha512Trunc256Sum([(i as u8).wrapping_add(1); 32]));
        let message_bytes = message.serialize_to_vec();
        let mut chunk = StackerDBChunkData::new(i as u32, 1, message_bytes);
        chunk.sign(&privk).unwrap();

        let chunk_event = StackerDBChunksEvent {
            contract_id: contract_id.clone(),
            modified_slots: vec![chunk],
        };
        chunks.push(chunk_event);
    }

    let thread_chunks = chunks.clone();

    // simulate a node that's trying to push data
    let mock_stacks_node = thread::spawn(move || {
        let mut num_sent = 0;
        while num_sent < thread_chunks.len() {
            let mut sock = match TcpStream::connect(endpoint) {
                Ok(sock) => sock,
                Err(..) => {
                    sleep_ms(100);
                    continue;
                }
            };

            let ev = &thread_chunks[num_sent];
            let body = serde_json::to_string(ev).unwrap();
            let req = format!(
                "POST /stackerdb_chunks HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{}",
                endpoint,
                body.len(),
                body
            );
            debug!("Send:\n{}", &req);

            sock.write_all(req.as_bytes()).unwrap();
            sock.flush().unwrap();

            num_sent += 1;
        }
```
