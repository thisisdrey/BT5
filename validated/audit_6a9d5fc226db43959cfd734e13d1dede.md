### Title
`handle_unsolicited_StackerDBPushChunk` advertises a chunk's version as accepted before it is actually committed to storage - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` patches the outgoing `StackerDBChunkInv` reply's `slot_versions` entry to the pushed chunk's version as soon as `validate_received_chunk` succeeds, and (when `send_reply` is set) immediately signs and sends this inventory back to the remote peer over p2p — before the chunk itself is written to the sqlite-backed `StackerDBs` store. Storage only happens later, when the caller relays the message onward for processing (`Ok((_, true))`) via the `process_stacker_db_chunks` path in `relay.rs`, which performs its own `try_replace_chunk` and can still fail or be skipped for reasons independent of the earlier validation (e.g., a concurrently-processed chunk for the same slot/version already committed, `NoSuchSlot`/DB errors, or the relay path simply dropping/deferring the chunk).

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `validate_received_chunk` (lines 649–718) checks size, expected slot/version, signature, staleness, and `max_writes`, but explicitly does **not** perform the actual write — the doc comment states it only validates "either downloaded... or pushed to us." `handle_unsolicited_StackerDBPushChunk` (lines 742–871) then does: [1](#0-0) 

i.e., after only *validating* the chunk, it patches the local copy of the inventory (`data.slot_versions`) to the new version and (unless `send_reply` is false) signs and transmits this inventory as a P2P reply to the sender: [2](#0-1) 

The actual persistence of the chunk (`StackerDBTx::try_replace_chunk`) happens only later, in `process_stacker_db_chunks` in `stackslib/src/net/relay.rs`, once the message is forwarded to the relayer for processing: [3](#0-2) 

Because the advertised inventory version is bumped in `handle_unsolicited_StackerDBPushChunk` *before* `try_replace_chunk` is ever called in the relay path, there is a window — and a set of failure paths (`try_replace_chunk` returning `StaleChunk`, `NoSuchSlot`, DB errors, or the sync transaction failing to commit) — in which the node has already told a neighbor "I have version N of slot S" while its on-disk `chunks` table still holds an older (or no) version. This breaks the served-vs-committed equality: the `StackerDBChunkInv` a node serves as canonical is not backed by what is actually committed to its `StackerDBs` sqlite store.

### Impact Explanation
This matches the "High - steering a node off the tip via false inventory" category. StackerDB inventories are used by `StackerDBSync` to schedule downloads on a rarest-first, newest-first basis (per the module doc comment at `stackslib/src/net/stackerdb/mod.rs:85-96`). A neighbor that receives this falsely-advanced inventory will believe the peer already has the new chunk version and may deprioritize re-fetching/re-broadcasting it to that peer, or treat that peer as an authoritative source for that version, when in fact the peer's local store has not committed it (and, on a subsequent GetChunk request, may return stale data or fail). This is a purely remote, unauthenticated interaction (any connected peer can send an unsolicited `StackerDBPushChunk`), and it requires no privileged key beyond a chunk that passes signature/version checks — of which validation is decoupled from storage.

### Likelihood Explanation
Likelihood is limited by the fact that `try_replace_chunk`'s checks (signer, staleness, max_writes) largely mirror `validate_received_chunk`'s checks, so under normal single-threaded processing the later store attempt usually succeeds. However, the two operations are not atomic with respect to each other: relay processing batches multiple `StackerDBSyncResult`s and can encounter races (e.g., two competing valid pushes for the same slot, one from unsolicited push and one from active sync, processed out of order) or transaction-level failures (`tx.commit()` failing after already broadcasting/acking) that create a real, remotely triggerable, divergence window rather than a purely theoretical one.

### Recommendation
Do not patch/advertise the inventory version, and do not sign/send the `StackerDBChunkInv` reply for a pushed chunk until the chunk has actually been durably committed via `try_replace_chunk`/`tx.commit()`. Move the inventory-patching and reply-signing logic in `handle_unsolicited_StackerDBPushChunk` (or defer it) to occur strictly after the corresponding storage write commits in `process_stacker_db_chunks`, so that any `StackerDBChunkInv` served as canonical always reflects state that is truly committed on disk.

### Proof of Concept
1. Attacker (or any connected peer) crafts a valid, properly-signed `StackerDBPushChunkData` for slot `S` with version `N` for a contract the victim replicates, matching the victim's current chain view (`rc_consensus_hash`).
2. Victim's `handle_unsolicited_StackerDBPushChunk` runs `validate_received_chunk`, which succeeds (size/version/signature/staleness/max_writes all pass) — see `stackslib/src/net/stackerdb/mod.rs:649-718`.
3. Victim patches `slot_versions[S] = N` in the `StackerDBChunkInv` payload and, if `send_reply` is true, immediately signs and sends this inventory back over p2p — `stackslib/src/net/stackerdb/mod.rs:784-807, 858-871` — before any `try_replace_chunk` call has run.
4. Separately/concurrently, the actual store attempt for this chunk (which happens later, in `process_stacker_db_chunks`, `stackslib/src/net/relay.rs:2406-2437`) fails or is skipped (e.g., a competing valid chunk for the same slot commits first, causing `StaleChunk`, or a DB/transaction error occurs).
5. Result: the victim has already advertised version `N` for slot `S` as its inventory to the requesting peer, but its `chunks` table never actually reflects version `N` — any peer relying on that inventory for scheduling or verification is working off state the victim never committed.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L784-807)
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
