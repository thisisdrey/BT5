### Title
False StackerDB Chunk Inventory Advertised Before Chunk Is Actually Stored - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` optimistically patches the `slot_versions` entry of the `StackerDBChunkInv` reply to reflect the *incoming* pushed chunk's version and immediately sends that reply back to the peer, **before** the chunk has actually been written to the local StackerDB. The real, authoritative write happens later and asynchronously, when the relayer drains `pushed_stackerdb_chunks` via `Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks` → `StackerDBTx::try_replace_chunk`. This creates a window in which the locally-advertised inventory (an externally observable, "served" piece of state) can diverge from what is truly stored, breaking the served-vs-stored equality that StackerDB inventory exchange depends on.

### Finding Description
In `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 
the code runs `validate_received_chunk` (structural/signature/staleness checks only — it does not write anything to the DB) and then directly mutates the outgoing `StackerDBChunkInv.slot_versions[slot_id]` to the pushed chunk's `slot_version`, assuming the chunk will be stored. It then falls through to: [2](#0-1) 
which signs and sends this patched inventory reply to the remote peer right away, and separately returns `(false, true)` so the *actual* chunk bytes are merely forwarded to the relayer for asynchronous processing (`self.pushed_stackerdb_chunks` is drained later in `consume_unsolicited` → `process_pushed_stacker_db_chunks`): [3](#0-2) [4](#0-3) 

The actual persistence goes through `try_replace_chunk`, which re-validates against the *current* on-disk `slot_validation` state (not the possibly-stale `data.slot_versions` array captured earlier) and can fail with `StaleChunk`, `TooManySlotWrites`, `BadSlotSigner`, or a DB error: [5](#0-4) 

Because the version-bump check in `validate_received_chunk` operates on a snapshot (`expected_versions`) taken at reply-construction time, and the durable write happens later against the then-current DB state, a race is possible: e.g., two chunks for the same slot (from two different peers, or a locally-generated write) can both pass `validate_received_chunk` against the same snapshot, but only one can win at `try_replace_chunk` time — the other becomes `StaleChunk` and is silently dropped (`continue` in `process_stacker_db_chunks`): [6](#0-5) 
Yet the P2P reply for the *losing* chunk has already told the sending peer "I now have slot_version = N", even though the local replica never actually stores that version. Any third peer that later asks this node for its `StackerDBChunkInv` will see the pre-race, real (unbumped) version, exposing the mismatch — but in the meantime the sender that received the false ack may stop retrying delivery of that chunk, believing it already succeeded.

### Impact Explanation
This falls into the "High" impact bucket described in the rules: serving non-canonical/aspirational state as canonical via the StackerDB inventory mechanism, which neighbors use to decide whether to push/pull specific slot versions. A remote, unprivileged peer that races two pushes for the same slot (or races a push against the node's independent StackerDB sync) can cause the node to falsely acknowledge storage of a chunk it does not actually have, undermining the peer-to-peer chunk-propagation invariant that inventory reflects real stored state.

### Likelihood Explanation
Reaching this requires only sending unsolicited `StackerDBPushChunk` messages, which any P2P-connected peer can do; no signing key ownership over the *node* is needed (only over the *slot*, which for signer-message pushes belongs to the attacker's own signer key if they use their own slot, or the race can be triggered by any properly-signed chunk for a shared slot). The race window (between reply construction and asynchronous relayer processing) is real but requires precise timing, so likelihood is moderate rather than trivially guaranteed on every push.

### Recommendation
Do not patch and send the `StackerDBChunkInv` reply based on an assumption that the chunk will be stored. Either (a) perform the actual `try_replace_chunk` write synchronously before constructing the reply and only patch `slot_versions` on confirmed success, or (b) defer sending the `StackerDBChunkInv` reply until after the relayer has processed `pushed_stackerdb_chunks` and can report the true, post-write slot version.

### Proof of Concept
1. Attacker (or racing legitimate traffic) sends two `StackerDBPushChunk` messages for the same `contract_id`/`slot_id` with a higher `slot_version` than what's currently stored, from two different established P2P connections, so `event_id` differs but they arrive to `handle_unsolicited_StackerDBPushChunk` back-to-back before either is committed to the DB.
2. Both calls independently invoke `make_StackerDBChunksInv_or_Nack`, which computes `slot_versions` from the same pre-write snapshot; both pass `validate_received_chunk` against that snapshot, so both patch their respective `StackerDBChunkInv` replies to claim the new (higher) version and immediately send those replies to their respective peers via `send_p2p_message`.
3. Both `StackerDBPushChunkData` are queued into `self.pushed_stackerdb_chunks` and only later drained by `Relayer::process_pushed_stacker_db_chunks`, which calls `try_replace_chunk` for each in turn; the first call succeeds and bumps the on-disk version, the second call now fails with `StaleChunk` and is dropped with only a debug log (`stackslib/src/net/relay.rs:2412-2437`).
4. The peer that sent the second (dropped) chunk has already received an inventory reply claiming success — the local node now advertises inventory state that does not match what is actually persisted, and that peer will not resend the chunk.

### Citations

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

**File:** stackslib/src/net/mod.rs (L2249-2254)
```rust
                    StacksMessageType::StackerDBPushChunk(chunk_data) => {
                        self.pushed_stackerdb_chunks.push(PushedStackerDBChunk {
                            peer: neighbor_addr.clone(),
                            chunk: chunk_data,
                        })
                    }
```

**File:** stackslib/src/net/relay.rs (L2405-2437)
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
