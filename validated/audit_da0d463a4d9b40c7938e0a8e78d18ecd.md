### Title
`handle_unsolicited_StackerDBPushChunk` advertises a pushed chunk version as stored in its inventory reply before (and even if) the chunk is ever actually committed to the StackerDB - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` patches the `StackerDBChunkInv` reply it sends back to a pushing peer to claim the pushed chunk's `slot_version` is now locally available, immediately after `validate_received_chunk` succeeds — but *before* the chunk is actually written to the on-disk StackerDB. The actual write happens later, asynchronously, via the relayer's `process_stacker_db_chunks`, which can silently drop the chunk (e.g. `StaleChunk` from a concurrent write, or any other DB error) without ever updating the acked inventory. This breaks the "advertised/served inventory vs. actually committed data" equality: the reply can tell a peer (and, by proxy, anything that trusts this node's stated inventory) that a given chunk version is replicated here when it is not, and in the failure case never will be.

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk` builds a normal `StackerDBChunksInv` reply via `make_StackerDBChunksInv_or_Nack`, then runs `validate_received_chunk` (signature, size, version, max-writes checks) against the *current* `slot_versions` snapshot: [1](#0-0) 

Once validation passes, the code optimistically patches the outbound inventory to reflect that the new version is stored ("we'll accept this chunk") and wakes up the sync state machine — but no database write has occurred yet at this point: [2](#0-1) 

The docstring for this function explicitly acknowledges the message is only queued for later processing and that mutating shared state here must be done carefully: [3](#0-2) 

The actual persistence happens later and separately, in the relayer's `process_stacker_db_chunks`, which calls `try_replace_chunk` per chunk and, on any failure (including a `StaleChunk` race against a concurrently-processed write to the same slot, or any other DB error), simply logs and `continue`s — the chunk is dropped and never stored: [4](#0-3) 

`try_replace_chunk` itself re-validates signer, staleness, and write-count against the DB's authoritative `SlotValidation` record at commit time: [5](#0-4) 

Because the inventory patch in `handle_unsolicited_StackerDBPushChunk` happens against an earlier snapshot (`validate_received_chunk`'s `expected_versions` argument, taken from `make_StackerDBChunksInv_or_Nack`'s `get_slot_versions` call) and is not gated on the later, authoritative `try_replace_chunk` outcome, there exists a window — and in the DB-error/stale-race case, a permanent state — where the node has told its peer "I now have version V of this chunk" while the actual on-disk `SlotValidation.version` for that slot is still the old value. Any process that trusts the acked/patched inventory (the sender itself, or the sync state machine woken up to schedule further pulls/pushes based on `chunk_invs`) is working off non-canonical state presented as canonical.

### Impact Explanation
This is a "serving non-canonical state as canonical" defect (High, per the scoring rubric): a StackerDB replica can report chunk versions in its P2P-facing inventory that do not match what it actually has stored, purely as a side effect of normal, unprivileged StackerDB chunk pushes and ordinary concurrent-write races or transient DB errors — no authentication bypass or forged signature is needed. Downstream consumers of the inventory (the peer's own `chunk_invs`-driven sync scheduling, described in the module doc as using inventories to prioritize newest-first/rarest-first replication) can be misled into thinking a chunk is already replicated at this node and skip fetching/re-broadcasting it, degrading replication guarantees and potentially causing "stuck" or missing data for consumers of the StackerDB contract (e.g. signer message sets) that rely on eventual full replication.

### Likelihood Explanation
This does not require attacker privileges beyond being a normal StackerDB writer able to push a validly-signed chunk — a scenario that happens routinely (e.g. any signer client via `stacks-signer/src/client/stackerdb.rs`). The race window is opened simply by two chunks for the same slot being pushed/received close together, or any transient error in `try_replace_chunk` (e.g. sqlite busy/lock contention under load), both of which are ordinary operational conditions rather than a crafted exploit, making the trigger likely in a moderately active network.

### Recommendation
Only patch/report the advertised `slot_version` in the `StackerDBChunkInv` reply after the chunk has actually been durably committed via `try_replace_chunk` (i.e., move the inventory-patching and any reliance on "we now have this chunk" to occur post-commit, or have the relayer re-derive and send the ack/inventory update from the authoritative DB state after `process_stacker_db_chunks` completes, instead of optimistically mutating the pre-write snapshot in `handle_unsolicited_StackerDBPushChunk`).

### Proof of Concept
1. Peer A sends a validly-signed `StackerDBPushChunkData` for slot X, version V to node N.
2. N's `handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk` against its currently-known `slot_versions` (version V-1 stored), which succeeds, and patches the reply's `slot_versions[X] = V`, sending this back to A and waking the sync state machine — all before any DB write for this chunk.
3. Concurrently (or immediately prior), another push/sync path for the same slot X already advanced the stored version to V or beyond (harmless in isolation, but timing-dependent here), or the subsequent `try_replace_chunk` call in `process_stacker_db_chunks` hits a transient error.
4. `process_stacker_db_chunks` catches the `StaleChunk`/error from `try_replace_chunk`, logs it, and `continue`s — the chunk for version V is never stored.
5. N's on-disk `SlotValidation.version` for slot X remains at its prior value, yet N has already told peer A (and its own internal `chunk_invs`/sync bookkeeping was nudged awake believing it) that version V is present — a mismatch between advertised/served state and actual committed state.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L720-733)
```rust
    /// Handle unsolicited StackerDBPushChunk messages.
    /// Check to see that the message can be stored or buffered.
    ///
    /// Optionally, make a reply handle for a StackerDBChunksInv to be sent to the remote peer, in which
    /// the inventory vector is updated with this chunk's data.  Or, send a NACK if the chunk
    /// cannot be buffered or stored.
    ///
    /// Note that this can happen *during* a StackerDB sync's execution, so be very careful about
    /// modifying a state machine's contents!  The only modification possible here is to wakeup
    /// the state machine in case it's asleep (i.e. blocked on waiting for the next sync round).
    ///
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
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
