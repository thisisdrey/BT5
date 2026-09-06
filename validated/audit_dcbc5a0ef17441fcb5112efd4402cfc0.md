### Title
Unsolicited StackerDB chunk inventory advertised as accepted before the chunk is actually persisted — ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an unsolicited `StackerDBPushChunk` and then immediately patches its outgoing `StackerDBChunkInv` reply to report the *new* (pushed) slot version to the sending peer, and — via its `(false, true)` return — hands the chunk off to the relayer thread for actual storage. The reply that announces "I now have version N of this slot" is signed and sent to the network before the chunk is committed to the local `StackerDBs` store, which only happens later, asynchronously, when the relayer calls `Relayer::process_stacker_db_chunks` → `StackerDBTx::try_replace_chunk`. This is the same class of bug as the reported `poolManager.sync`-before-`unlock` issue: an operation that depends on a later "commit" step is performed and externally observable *before* that commit occurs.

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 

1. `self.validate_received_chunk(...)` checks size, signer signature, staleness, and max-writes — but this is a **stateless** check against the *currently known* inventory (`data.slot_versions`), not a check that the chunk has been written to disk.
2. On success, the code directly mutates the outgoing `StackerDBChunkInv` payload: `*slot_version = chunk_data.chunk_data.slot_version;` — i.e., it reports to the network that this replica now holds the new version.
3. Immediately after, if `send_reply` is set, this patched inventory is signed and sent back to the peer as an authoritative P2P reply: [2](#0-1) 

4. The function returns `(false, true)` — "don't buffer, do forward to relayer" — meaning the *actual* write of the chunk bytes into the `StackerDBs` sqlite store happens later, out-of-band, in `Relayer::process_stacker_db_chunks`: [3](#0-2) 

Here `tx.try_replace_chunk` performs the real, authoritative validation (owner-signature re-check, staleness, `max_writes`) and only actually inserts the row; the whole batch is committed with `tx.commit()?` at the very end. Crucially, the p2p-side "announce we have it" (step 2/3 above) has zero coupling to whether this later transaction ever succeeds or commits. Any peer receiving the immediate `StackerDBChunkInv` reply believes the local node's inventory equals the *not-yet-committed* database state.

This breaks the equality that the StackerDB inventory protocol depends on: "advertised inventory version == what is actually stored and fetchable via `StackerDBGetChunkData`." The doc-comment for the module explicitly states this invariant: [4](#0-3) 

### Impact Explanation
Because the inventory patch and its signed announcement happen strictly before the durable write, there is a window (and possible permanent divergence if the deferred `try_replace_chunk` later fails — e.g. `StaleChunk`/`TooManySlotWrites`/`BadSlotSigner` due to a race with a concurrently-processed chunk for the same slot, or if the relayer never gets around to it before a crash) in which the local node's advertised StackerDB inventory says it has data it does not actually have. Downstream peers use `StackerDBChunkInv`/version data to schedule fetches (rarest-first, newest-first), per the module’s own design description: [5](#0-4) 

A peer that trusts this advertisement will request the chunk from this node and get a failed/`Nack`/stale response, wasting a round-trip and potentially stalling synchronization of that slot if the peer treats this node as the authoritative rarest replica. This is a "served vs. committed" mismatch analogous to the reported class (serving non-canonical/uncommitted state as if canonical). It is remotely triggerable by any connected, unauthenticated-but-handshaked peer that can push a validly-signed `StackerDBPushChunk` for a slot it does not own the write for concurrently with another writer, or simply by racing pushes.

### Likelihood Explanation
Likelihood is moderate: it requires only a normal, validly-signed `StackerDBPushChunk` message (no special crafting needed) and a normal condition where the deferred `try_replace_chunk` call in the relayer diverges from the earlier "in-line" validation — e.g., two different valid pushers racing for the same slot (a common, expected scenario per the `StaleChunk` handling code already present in `relay.rs`), or a relayer processing delay under load. It does not require compromising any keys.

### Recommendation
Do not construct/send the `StackerDBChunkInv` reply with the "we now have version N" claim until the chunk has actually been durably persisted by the relayer. Options:
- Defer sending the P2P inventory reply until after `Relayer::process_stacker_db_chunks` (or an equivalent synchronous store path) confirms the chunk was written; or
- Perform the actual `try_replace_chunk` write synchronously inside `handle_unsolicited_StackerDBPushChunk` (as is effectively done for the HTTP-upload path in `poststackerdbchunk.rs`, which commits before acknowledging) and only patch/send the inventory after a successful commit.

### Proof of Concept
1. Two well-formed, validly-signed `StackerDBChunkData` payloads targeting the same `(contract_id, slot_id)` are pushed to a target node in quick succession from two different connections (each an unsolicited `StackerDBPushChunk`).
2. For the first message, `handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk` (passes), patches `data.slot_versions[slot_id]` to the pushed version, signs and sends the `StackerDBChunkInv` reply advertising the new version, and returns `(false, true)` to forward to the relayer.
3. Before the relayer thread actually calls `try_replace_chunk` for message 1, message 2 arrives and is processed the same way against the same (still-unmodified) in-memory `slot_versions` baseline, also advertising an updated version.
4. When the relayer eventually processes both queued messages via `Relayer::process_stacker_db_chunks`, only one `try_replace_chunk` call succeeds; the other returns `Error::StaleChunk` and is dropped (logged, not stored) — see the drop path at: [6](#0-5) 
5. The peer that received the inventory reply corresponding to the dropped chunk now believes this node holds a slot version that was never actually stored; subsequent `StackerDBGetChunkData` requests for that version will fail or return stale data, demonstrating the "advertised vs. committed" mismatch.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L87-96)
```rust
/// The DB inventory (`StackerDBChunkInvData`) is simply a vector of all of the remote peers' slots' versions.
/// Once the node has received all DB inventories from its neighbors, it schedules them for
/// download by prioritizing them by newest-first, and then by rarest-first, in order to ensure
/// that the latest, least-replicated data is downloaded first.
///
/// Once the node has computed its download schedule, it queries its DB neighbors for chunks with
/// the given versions (via `StackerDBGetChunkData`).  Upon receipt of a chunk, the node verifies the signature on the chunk's
/// metadata (via `SlotMetadata`), verifies that the chunk data hashes to the metadata's indicated data hash, and stores
/// the chunk (via `StackerDBSet` and `StackerDBTx`).  It will then select neighbors to which to broadcast this chunk, inferring from the
/// download schedule which DB neighbors have yet to process this particular version of the chunk.
```

**File:** stackslib/src/net/stackerdb/mod.rs (L785-814)
```rust
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
