### Title
Unsolicited `StackerDBPushChunk` Handler Advertises a Chunk Version as Stored Before It Is Actually Persisted, Creating a Served-vs-Committed State Inconsistency - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an incoming chunk, patches its in-memory `StackerDBChunkInvData` to reflect the chunk as accepted, and immediately sends that inventory back to the sender as an acknowledgement — all *before* the chunk is actually written to the StackerDB. The real, authoritative write happens later and separately, in the relayer (`Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks` → `StackerDBTx::try_replace_chunk`), which re-checks the on-disk version and can silently drop the very same chunk as stale. This breaks the equality between what the node *told a peer it has* (served inventory) and what it *actually committed* to its StackerDB.

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk` builds a reply payload via `make_StackerDBChunksInv_or_Nack`, then validates the pushed chunk with `validate_received_chunk` and patches the local copy of `slot_versions` to reflect the new version as accepted: [1](#0-0) 

It then immediately signs and sends this patched `StackerDBChunkInv` back over the wire to the sender, and returns `(false, true)` — meaning "don't buffer, forward to the relayer" — with a comment stating "we can store it right now": [2](#0-1) 

However, the actual persistence of the chunk does **not** happen here. It happens later, asynchronously, in the relayer: [3](#0-2) 

which calls `try_replace_chunk`, re-validating against the *current on-disk* slot version and silently dropping stale/duplicate writes with only a debug log — no corrective NACK is ever sent to the peer that already received the earlier "accepted" ack: [4](#0-3) 

The authoritative version check lives in `try_replace_chunk` itself, which enforces strict monotonicity of `slot_version` against the currently stored `slot_validation.version`: [5](#0-4) 

Because the p2p-layer ack (`handle_unsolicited_StackerDBPushChunk`) and the relayer's real DB write (`try_replace_chunk`) are two independent stages operating on the same mutable per-slot version counter without atomicity between "ack" and "commit," any two properly-signed pushes for the same slot arriving on different connections/event IDs before either is flushed to disk will each independently pass `validate_received_chunk` (both compare against the same stale `expected_versions` snapshot) and each cause an "accepted" `StackerDBChunkInv` to be sent to their respective sender. When the relayer subsequently processes both, only the higher version is actually committed via `try_replace_chunk`'s strict `slot_version <= slot_validation.version` check; the other is dropped as `net_error::StaleChunk`. The peer that sent the dropped one was already told, via the earlier Inv reply, that its chunk was accepted.

### Impact Explanation
This is a remote, unauthenticated ability to make a node's advertised StackerDB inventory diverge from its actually committed StackerDB state — a "served vs. committed" equality violation. Downstream effects:
- The misled peer believes its write succeeded and may not retry, permanently losing that chunk from the network's perspective while the local node's own inventory (once corrected) no longer matches what it told the peer.
- The node itself may subsequently gossip an inventory (from `make_StackerDBChunksInv_or_Nack` reflecting the on-disk `get_slot_versions`) that contradicts what it previously claimed to the racing peer, confusing sync scheduling in `StackerDBSync` state machines across the network (nodes deciding not to re-fetch/re-push data they believe is already replicated).
- No signer key, node secret, or privileged role is required — any two registered StackerDB signers (or a single one using two connections) can trigger the race with ordinary, validly-signed chunk pushes.

This matches the "High: steering a node off the tip via false inventory" class from the rules, applied to StackerDB chunk inventory instead of block inventory.

### Likelihood Explanation
Triggering the race only requires two StackerDB pushes for the same slot to be handled by `handle_unsolicited_StackerDBPushChunk` on two different `event_id`s before the relayer's next `process_network_result` pass commits either one. Since P2P message handling and relayer processing are decoupled stages of the event loop (`NetworkResult` accumulated across a poll cycle then handed to `Relayer::process_network_result`), this window is easily reachable in normal network conditions with two near-simultaneous pushes — no bandwidth flooding or volumetric attack needed, just message timing.

### Recommendation
Do not send an "accepted" `StackerDBChunkInv` acknowledgement from `handle_unsolicited_StackerDBPushChunk` until the chunk has actually been durably written via `try_replace_chunk`. Alternatively, perform the version-conflict check and the actual DB write atomically within the same handler (holding the necessary lock/transaction) rather than deferring the write to a separate relayer pass that re-validates against a state that may have changed since the ack was already sent.

### Proof of Concept
1. Configure a StackerDB slot owned by signer `S` with current on-disk version `V0`.
2. Have `S` produce two validly-signed `StackerDBChunkData` for the slot: version `V1 = V0+1` and `V2 = V0+2`.
3. Push `V2` over connection/event A and `V1` over connection/event B to the victim node in quick succession, both before the relayer's next `process_network_result` cycle runs.
4. Both invocations of `handle_unsolicited_StackerDBPushChunk` read the same stale `expected_versions` (still showing `V0`), both pass `validate_received_chunk`, and each sends back an "accepted" `StackerDBChunkInv` reflecting `V2` and `V1` respectively to their own connection.
5. When the relayer later calls `process_stacker_db_chunks` → `try_replace_chunk` for both queued chunks, only the higher version (`V2`, assuming it's processed) is committed; the `V1` push is silently dropped as `StaleChunk` per `stackslib/src/net/stackerdb/db.rs` lines 424-429 — yet the sender of `V1` already received an "accepted" ack from step 4, producing the served-vs-committed divergence.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L784-815)
```rust
                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
                }

                // patch inventory -- we'll accept this chunk
                let Some(slot_version) = data
                    .slot_versions
                    .get_mut(chunk_data.chunk_data.slot_id as usize)
                else {
                    error!(
                        "Chunk not accepted with slot_id {}, which is greater than our slot_versions array {} in {}",
                        chunk_data.chunk_data.slot_id,
                        data.slot_versions.len(),
                        chunk_data.contract_id
                    );
                    return Ok((false, false));
                };
                *slot_version = chunk_data.chunk_data.slot_version;

                // wake up the state machine -- force it to begin a new sync if it's asleep
                if let Some(stackerdb_syncs) = self.stacker_db_syncs.as_mut() {
                    if let Some(stackerdb_sync) = stackerdb_syncs.get_mut(&chunk_data.contract_id) {
                        stackerdb_sync.wakeup();
                    }
                }
            }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L858-871)
```rust
        if !send_reply {
            return Ok((false, true));
        }

        // this is a reply to the pushed chunk, and we can store it right now (so don't buffer it)
        let resp = self.sign_for_p2p_reply(event_id, preamble.seq, payload)?;
        let handle = self.send_p2p_message(
            event_id,
            resp,
            self.connection_opts.neighbor_request_timeout,
        )?;
        self.add_relay_handle(event_id, handle);
        Ok((false, true))
    }
```

**File:** stackslib/src/net/relay.rs (L2409-2437)
```rust
                for sync_result in sync_results.into_iter() {
                    for (origin, chunk) in sync_result.chunks_to_store.into_iter() {
                        let md = chunk.get_slot_metadata();
                        if let Err(e) = tx.try_replace_chunk(&sc, &md, &chunk.data) {
                            if matches!(e, Error::StaleChunk { .. }) {
                                // This is a common and expected message, so log it as a debug and with a sep message
                                // to distinguish it from other message types.
                                debug!(
                                    "Dropping stale StackerDB chunk";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            } else {
                                warn!(
                                    "Failed to store chunk for StackerDB";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            }
                            continue;
                        } else {
                            log_stored_stackerdb_chunk(&sync_result.contract_id, &chunk, &origin);
                        }
```

**File:** stackslib/src/net/relay.rs (L2469-2493)
```rust
    /// Process StackerDB chunks pushed to us.
    /// extract all StackerDBPushChunk messages from `unhandled_messages`
    pub fn process_pushed_stacker_db_chunks(
        &mut self,
        rc_consensus_hash: &ConsensusHash,
        stackerdb_configs: &HashMap<QualifiedContractIdentifier, StackerDBConfig>,
        stackerdb_chunks: Vec<PushedStackerDBChunk>,
        event_observer: Option<&dyn StackerDBEventDispatcher>,
    ) -> Result<(), Error> {
        // synthesize StackerDBSyncResults from each chunk
        let sync_results = stackerdb_chunks
            .into_iter()
            .map(|pushed| {
                debug!("Received pushed StackerDB chunk {:?}", pushed.chunk);
                StackerDBSyncResult::from_pushed_chunk(pushed.chunk, pushed.peer)
            })
            .collect();

        self.process_stacker_db_chunks(
            rc_consensus_hash,
            stackerdb_configs,
            sync_results,
            event_observer,
        )
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L400-438)
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
        if slot_desc.slot_version <= slot_validation.version {
            return Err(net_error::StaleChunk {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
            });
        }
        if slot_desc.slot_version > self.config.max_writes {
            return Err(net_error::TooManySlotWrites {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
                max_writes: self.config.max_writes,
            });
        }
        self.insert_chunk(smart_contract, slot_desc, chunk)
    }
```
