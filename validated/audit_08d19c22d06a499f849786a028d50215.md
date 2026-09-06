### Title
Unsolicited `StackerDBPushChunk` handling advertises an accepted slot version before the chunk is actually stored, producing a false/non-canonical inventory reply - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` patches the local `StackerDBChunkInv` reply to claim a new, higher `slot_version` for a pushed chunk as soon as the chunk passes signature/version/size validation — but it never actually persists that chunk in this code path. It only "wakes up" the StackerDB sync state machine to fetch/store it later. If `send_reply` is set, this patched, signed inventory is sent straight back to the remote peer as an authoritative statement of what this node currently holds, even though the chunk has not yet been written to the local StackerDB.

### Finding Description
This mirrors the Arcadia bug class exactly: a value (there, cached Uniswap liquidity; here, the advertised `slot_version` in the chunk inventory) is recorded as "accepted"/"current" before the underlying state-changing operation (writing the chunk into the StackerDB) has actually completed.

In `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 

1. `make_StackerDBChunksInv_or_Nack` builds the base inventory reply from the on-disk state.
2. `validate_received_chunk` confirms only that the pushed chunk is well-formed and properly signed for the *claimed* version — it does not write anything to the database.
3. Immediately afterward, the code patches `data.slot_versions[slot_id] = chunk_data.chunk_data.slot_version` — i.e., the reply now claims the node already has the new version.
4. The only side effect actually performed is `stackerdb_sync.wakeup()`, which merely nudges the StackerDB sync state machine to eventually try to download/store the data through the normal `validate_downloaded_chunk` → `try_replace_chunk` path.
5. If `send_reply` is true, this patched inventory is signed and sent back to the peer as the node's official state: [2](#0-1) 

The actual, durable chunk write only happens later and separately, in `Relayer::process_stacker_db_chunks` via `StackerDBTx::try_replace_chunk`, driven by the sync machine's own download round: [3](#0-2) [4](#0-3) 

Between step 3 (advertise) and the eventual completion of the sync round that actually stores the chunk, the node's advertised StackerDB inventory does not match its persisted StackerDB state. Any peer that queries this node for that slot/version in that window (via `StackerDBGetChunk`) will either get a stale/older chunk or a failure, contradicting the version this node just vouched for in a signed reply.

### Impact Explanation
This breaks the equality "advertised/served inventory state == actually stored state," which the report explicitly calls out as in-scope ("non-canonical state served as canonical"). A remote, unprivileged peer can push any well-signed StackerDB chunk (properly signed by the real slot owner is required — an attacker cannot forge arbitrary content into other signers' slots, but simply *relaying* or replaying an already-signed chunk from the network is enough) to trigger this node into emitting a signed inventory claiming persisted state it does not yet have. This can mislead other nodes' rarest-first download scheduling (they may treat this node as already holding the data and deprioritize fetching/relaying it elsewhere), and momentarily serves non-canonical inventory as canonical to the network.

### Likelihood Explanation
The trigger is trivial and fully remote/unprivileged: any peer with an established P2P conversation can push a `StackerDBPushChunk` message with a validly-signed chunk (which can simply be a chunk they observed elsewhere, no need to forge a new signature) to any node holding that StackerDB replica. No special privilege beyond forming a P2P connection is required, and the mismatch window exists on every unsolicited push whose sync round hasn't yet run to completion.

### Recommendation
Do not patch/emit the advertised `slot_version` in the `StackerDBChunkInv` reply until the chunk has actually been committed via `StackerDBTx::try_replace_chunk` (e.g., store it synchronously here, or only reflect the previously-persisted version in the reply and let the *next* inventory poll reflect the new version once storage completes asynchronously).

### Proof of Concept
1. Establish a P2P conversation with a target node that replicates a known StackerDB contract.
2. Obtain any legitimately-signed `StackerDBChunkData` for that contract (e.g., replay one seen from gossip) with a `slot_version` higher than the target's currently stored version.
3. Send it as an unsolicited `StackerDBPushChunk` message with `send_reply = true`.
4. Observe that `handle_unsolicited_StackerDBPushChunk` (`stackslib/src/net/stackerdb/mod.rs:794-814`) patches `slot_versions[slot_id]` to the new version and, per lines 858-870, signs and returns a `StackerDBChunkInv` claiming this version — all before any call to `try_replace_chunk`.
5. Immediately (before the sync state machine's wakeup round completes and actually stores it), issue a `StackerDBGetChunk` request for that slot/version to the same node; the node cannot serve it as claimed, demonstrating the inventory/state mismatch.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L761-814)
```rust
        let mut payload = self.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &chunk_data.contract_id,
            &chunk_data.rc_consensus_hash,
        );
        match payload {
            StacksMessageType::StackerDBChunkInv(ref mut data) => {
                // this message corresponds to an existing DB, and comes from the same view of the
                // stacks chain tip
                let stackerdb_config = if let Some(config) =
                    self.get_stacker_db_configs().get(&chunk_data.contract_id)
                {
                    config
                } else {
                    // not for this DB
                    info!(
                        "StackerDBChunk for {} ID {} is not available locally",
                        &chunk_data.contract_id, chunk_data.chunk_data.slot_id
                    );
                    return Ok((false, false));
                };

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

**File:** stackslib/src/net/stackerdb/mod.rs (L858-870)
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

**File:** stackslib/src/net/stackerdb/db.rs (L398-438)
```rust
    /// Add or replace a chunk for a given reward cycle, if it is valid
    /// Otherwise, this errors out with Error::StaleChunk
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
