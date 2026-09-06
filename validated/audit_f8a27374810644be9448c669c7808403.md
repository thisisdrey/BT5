### Title
StackerDB push-chunk handler acknowledges a slot version before the chunk is durably written, allowing a false/stale inventory to be advertised and gossiped - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an incoming `StackerDBChunkInv`/push chunk and immediately patches the in-memory inventory it is about to send back to the sender as an acknowledgment, claiming the new `slot_version` is now held locally. The actual, durable write of that chunk to the `StackerDBs` sqlite store happens later and separately, in `Relayer::process_stacker_db_chunks` (reached via `process_pushed_stacker_db_chunks`), after this message has been forwarded out of the unsolicited-message path. Because the ack is generated before the corresponding `try_replace_chunk` write is attempted, the two steps can diverge: the ack promises data the node does not (or may never) actually have on disk.

### Finding Description
`validate_received_chunk` [1](#0-0)  only checks chunk size, expected/prior version, signer identity, and `max_writes` against the *currently loaded* inventory snapshot (`expected_versions`)—it never writes anything.

Immediately after this validation succeeds, `handle_unsolicited_StackerDBPushChunk` patches the local `StackerDBChunkInv` reply object in memory to claim the new version is now present, and wakes the sync state machine, all without touching the database: [2](#0-1) 

That patched inventory is what gets sent back to the peer as the ack (`send_reply` / the returned `payload`) — i.e. the node tells the remote peer "I now have slot X at version N" before any write has been attempted.

The actual persistence of the pushed chunk happens later, in a wholly separate call chain: `handle_unsolicited_stacks_message` forwards the message to the relayer when `can_store` is true [3](#0-2) , which eventually reaches `Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks`, where `tx.try_replace_chunk` performs the real, authoritative signature/version re-check and DB write: [4](#0-3) 

Crucially, `try_replace_chunk` re-validates against whatever is on disk *at write time*, and can fail (`StaleChunk`, `BadSlotSigner`, `TooManySlotWrites`, etc.) even though `validate_received_chunk` succeeded earlier against a now-outdated in-memory snapshot: [5](#0-4) 
On failure, the chunk is silently dropped with only a debug/warn log — no correction is sent to the peer that already received the acknowledgment: [6](#0-5) 

This is exactly the class of bug described in the reference report: a value that is *announced/credited* is computed from a state snapshot taken *before* a later state-mutating operation completes, so the announced value and the actually-committed value can diverge. Here, the equality broken is "advertised StackerDB inventory == actually stored StackerDB inventory."

### Impact Explanation
Any two neighbors (or even the same peer racing two connections) can push conflicting versions of the same slot in the same processing pass. Both pushes can pass `validate_received_chunk` against the stale on-disk snapshot and both get an ack claiming their version is now stored, but only one write ultimately survives `try_replace_chunk`'s stale-version check; the other is dropped. The node has now gossiped/acknowledged possession of a chunk version it does not actually hold. Any third party that relies on this node's `StackerDBChunkInv` (e.g., during `StackerDBSync` download-schedule computation, or a peer that skips downloading from elsewhere because this node claimed to have the latest version) can be led to believe stale/incorrect state is canonical — matching the "serving non-canonical state as canonical" / "steering a node off the tip via false inventory" High-impact category called out for this scan.

### Likelihood Explanation
This requires no privileged access, no signing key, and no protocol violation — just two ordinary, validly-signed pushes (from any signer/author with write access to a slot, e.g. a Nakamoto signer) racing against each other, or a push racing against a normal StackerDB sync download for the same slot. This is a routine, remotely triggerable event pattern (chunk pushes are frequent in the signer-set StackerDB use case), not a contrived edge case.

### Recommendation
Do not construct or send the "accepted" `StackerDBChunkInv` acknowledgment/patch the advertised inventory until *after* the chunk has actually been durably written via `try_replace_chunk`. Concretely, either:
- perform the actual `try_replace_chunk` write synchronously inside `handle_unsolicited_StackerDBPushChunk` before building the ack, and only patch/report the inventory entry on write success, or
- defer sending any ack/ChunksInv reply until the relayer's `process_stacker_db_chunks` write completes, and have that path report the true post-write versions rather than relying on the earlier speculative patch.

### Proof of Concept
1. Two Stacks signers push chunk data for the same `(contract_id, slot_id)` at versions `N` and `N+1` respectively to victim node `V`, arriving in the same network processing iteration (e.g., near-simultaneous P2P messages from two different peers/connections).
2. For each push, `V::handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk` against the same on-disk snapshot (`expected_versions` unchanged since neither write has occurred yet), and both pass since both are newer than what's currently on disk.
3. `V` patches its in-memory `StackerDBChunkInv` reply for each connection to report versions `N` and `N+1` respectively and sends both acks back to the two senders — both senders now believe `V` holds their pushed version.
4. Both pushes are then forwarded to `Relayer::process_stacker_db_chunks`, which calls `tx.try_replace_chunk` for each in turn. Say version `N+1` is applied first; the subsequent attempt to apply version `N` fails with `StaleChunk` and is silently dropped via `continue`.
5. The peer that pushed version `N` was told `V` accepted its chunk, but `V`'s persisted `slot_version` for that slot is actually `N+1`; if the true canonical value was supposed to be `N` (e.g., because the `N+1` sender was itself using stale/forged data that also passed signature checks), other peers querying `V`'s inventory will be served the wrong version as if it were authoritative, without any resync or correction signal being sent to the misled peer.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

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

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
```

**File:** stackslib/src/net/stackerdb/mod.rs (L784-814)
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
```

**File:** stackslib/src/net/unsolicited.rs (L548-576)
```rust
            StacksMessageType::StackerDBPushChunk(ref data) => {
                // N.B. send back a reply if we're calling to buffer, since this would be the first
                // time we're seeing this message (instead of a subsequent time on follow-up
                // processing).
                let (can_buffer, can_store) = self
                    .handle_unsolicited_StackerDBPushChunk(
                        chainstate, event_id, preamble, data, buffer,
                    )
                    .unwrap_or_else(|e| {
                        info!(
                            "{:?}: failed to handle unsolicited {:?} when buffer = {}: {:?}",
                            self.get_local_peer(),
                            payload,
                            buffer,
                            &e
                        );
                        (false, false)
                    });
                if buffer && can_buffer && !can_store {
                    debug!(
                        "{:?}: Buffering {:?} to retry on next sortition",
                        self.get_local_peer(),
                        &payload
                    );
                }
                (can_buffer, can_store)
            }
            _ => (false, true),
        }
```

**File:** stackslib/src/net/relay.rs (L2406-2455)
```rust
        for (sc, sync_results) in sync_results_map.into_iter() {
            if let Some(config) = stackerdb_configs.get(&sc) {
                let tx = self.stacker_dbs.tx_begin(config.clone())?;
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

                        if let Some(event_list) = all_events.get_mut(&sync_result.contract_id) {
                            event_list.push(chunk.clone());
                        } else {
                            all_events.insert(sync_result.contract_id.clone(), vec![chunk.clone()]);
                        }

                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
                    }
                }
                tx.commit()?;
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
