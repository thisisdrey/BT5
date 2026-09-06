### Title
StackerDB unsolicited-push handler advertises an updated chunk inventory before the chunk is actually validated/stored on disk - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` builds a `StackerDBChunkInv` reply from the *current* on-disk slot versions, then optimistically patches the in-memory `slot_versions` array to reflect the incoming, not-yet-persisted chunk before that chunk has actually been written by `StackerDBTx::try_replace_chunk`/`insert_chunk`. The reply that is (optionally) sent back to the peer reports a version bump the local node has not committed, breaking the "advertised vs. stored" equality — the same class of defect as H-6's "balance not updated" (state that is asserted/served does not match the state that was actually persisted).

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk` computes the reply payload via `make_StackerDBChunksInv_or_Nack`, which reads the *actual on-disk* `slot_versions` with `self.stackerdbs.get_slot_versions(contract_id)` [1](#0-0) .

It then runs `validate_received_chunk` (signature/version/size checks only — no DB write) and, on success, directly mutates the in-memory `data.slot_versions` entry to the *pushed* chunk's version:
```
*slot_version = chunk_data.chunk_data.slot_version;
``` [2](#0-1) 

At no point in this function is `StackerDBTx::try_replace_chunk` (the function that actually performs the `UPDATE chunks ... SET version = ...` write, and which additionally enforces write-frequency/max-writes) invoked. Actual storage of the chunk happens later and separately, in the relayer path (`process_stacker_db_chunks` in `stackslib/src/net/relay.rs`) after this handler returns `(true, true)`/`(false, true)` telling the caller whether to buffer/forward the message [3](#0-2) . That later `try_replace_chunk` call can still fail (e.g., `StaleChunk`, `TooManySlotWrites`, or a DB error), in which case nothing is ever written to the local slot [4](#0-3) .

If `send_reply` is true, the (patched) `StackerDBChunkInv` is sent straight back to the pushing peer, meaning the local node tells a remote peer "I now have version N of slot S" even though the corresponding write can still fail or has not yet occurred. This is precisely the class of bug flagged by H-6: the code path that "announces"/"credits" a new state (`slot_versions`) is decoupled from the code path that actually commits that state, so the two can diverge.

### Impact Explanation
A remote peer that receives this optimistic inventory reply will believe the node already replicates the newer chunk version and can act on that belief (e.g., stop pushing/re-requesting it, treat it as already propagated, or use it in a later `make_chunk_request_schedule`/`getchunksinv_try_finish` round that trusts inventories at face value). Meanwhile the node's actual replica may still be on the old version because the deferred `try_replace_chunk` failed (stale, too-many-writes, DB error) or the message was simply buffered and never processed. This causes the network to believe stale/non-existent data has propagated — a "served vs. committed" mismatch that can steer peers' sync state machines (`StackerDBSync`) into believing the chunk-set is more current than it is, i.e., serving non-canonical inventory as canonical. This falls under the accepted High-impact bucket ("steering a node off the tip via false inventory" / serving non-canonical state as canonical).

### Likelihood Explanation
Any remote, unprivileged peer can send an unsolicited `StackerDBPushChunk` message to a node it is connected to. Triggering the optimistic-but-unpersisted-write scenario only requires racing multiple pushes for the same slot (to hit `StaleChunk`/`TooManySlotWrites` in the deferred write) or transient local DB error conditions, both of which are plausible in normal multi-peer StackerDB traffic and do not require the node's own key or any privileged role.

### Recommendation
Do not construct/send the patched `StackerDBChunkInv` reply based on an assumption that the chunk will be stored. Either:
1. Perform the actual write (`try_replace_chunk`) synchronously inside `handle_unsolicited_StackerDBPushChunk` (respecting the function's existing constraints on not touching sync-state-machine internals) before patching/sending the inventory, or
2. Only report versions that reflect what is truly on disk (i.e., don't mutate `data.slot_versions` optimistically); let the reply reflect actual committed state, and have the relayer's subsequent successful write trigger a fresh, accurate inventory update/broadcast.

### Proof of Concept
1. Attacker (or any peer with `allowed = -1` handshake) connects to the victim node and completes a handshake for a StackerDB the victim replicates.
2. Attacker sends an unsolicited `StackerDBPushChunk` for slot `S` with `slot_version = N+1` and a valid signature from the slot's owning key (attacker does not need to own the key if it colludes with/observes a legitimately-signed chunk; the signature check in `validate_received_chunk` is satisfied by a validly-signed chunk regardless of whether the local DB will accept it).
3. `handle_unsolicited_StackerDBPushChunk` validates the chunk (signature/version/size only, no DB write), patches the in-memory inventory to `N+1`, and (if `send_reply`) immediately sends a `StackerDBChunkInv` back to the peer reporting version `N+1`.
4. Before the relayer's subsequent `process_stacker_db_chunks`/`try_replace_chunk` call executes, cause it to fail (e.g., have another peer push a conflicting/older-timestamped chunk for the same slot first, exhausting `max_writes`, or trigger a transient DB error) so the local database still stores version `N` (or nothing).
5. Query the victim's actual chunk via `StackerDBGetChunk`/`get_latest_chunk` for slot `S`: it returns version `N`, contradicting the `N+1` inventory advertised in step 3 — demonstrating the served-vs-committed mismatch.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L583-607)
```rust
    /// Create a StackerDBChunksInv, or a Nack if the requested DB isn't replicated here.
    /// Runs in response to a received StackerDBGetChunksInv or a StackerDBPushChunk
    pub fn make_StackerDBChunksInv_or_Nack(
        &self,
        naddr: NeighborAddress,
        chainstate: &mut StacksChainState,
        contract_id: &QualifiedContractIdentifier,
        rc_consensus_hash: &ConsensusHash,
    ) -> StacksMessageType {
        // N.B. check that the DB exists first, since we want to report StaleView only if the DB
        // exists
        let slot_versions = match self.stackerdbs.get_slot_versions(contract_id) {
            Ok(versions) => versions,
            Err(e) => {
                debug!(
                    "{:?}: failed to get chunk versions for {}: {:?}",
                    self.get_local_peer(),
                    contract_id,
                    &e
                );

                // most likely indicates that this DB doesn't exist
                return StacksMessageType::Nack(NackData::new(NackErrorCodes::NoSuchDB));
            }
        };
```

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

**File:** stackslib/src/net/relay.rs (L2406-2437)
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
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-437)
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
```
