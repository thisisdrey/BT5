### Title
StackerDB inventory is patched with the pushed chunk's version before storage is confirmed, causing the node to advertise data it does not actually hold - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` optimistically patches the outgoing `StackerDBChunkInv` reply's `slot_versions` entry with the *pushed* chunk's `slot_version` as soon as `validate_received_chunk` (signature/staleness/size checks) passes, and immediately sends that reply back to the peer — before the chunk has actually been written to the StackerDB (`try_replace_chunk`). Actual storage is deferred to a separate, later pipeline (`process_stacker_db_chunks` in `relay.rs`), which can fail or drop the chunk entirely (e.g. version race against a concurrently-processed chunk, or a stale `rc_consensus_hash` by the time the chunk reaches the relayer). This mirrors the Vault bug class: the accounting value (advertised inventory) is updated with the *requested/expected* outcome rather than the *confirmed* outcome, allowing state to diverge from reality.

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 
validates the pushed chunk only against in-memory `expected_versions` (signature, staleness, chunk size, max-writes) and, on success, directly mutates the `StackerDBChunkInv` reply's `slot_versions[slot_id]` to the new version — without having called `try_replace_chunk` or otherwise durably persisted the chunk.

That mutated inventory is then signed and sent back to the peer synchronously: [2](#0-1) 

The actual write only happens later, out-of-band, when the chunk is forwarded to the relayer and processed via `process_stacker_db_chunks`, which calls `StackerDBTx::try_replace_chunk`: [3](#0-2) 
If `try_replace_chunk` fails (e.g. `Error::StaleChunk` because another chunk for the same slot was committed first, which is possible under concurrent unsolicited pushes each validated against a snapshot of `expected_versions`), the loop simply logs and `continue`s — there is no mechanism to retract or correct the inventory reply that was already sent to the peer. Additionally, the project's own test suite documents that a pushed chunk can be silently dropped later if the node's view (`rc_consensus_hash`) has moved on by the time the network result is processed, and this drop happens *after* the optimistic inventory patch/ack has already gone out: [4](#0-3) 

This breaks the equality that should hold between "what a StackerDB replica claims (via `StackerDBChunkInv`) it holds" and "what is actually committed in its `StackerDBTx`/sqlite backing store" — precisely analogous to the Vault report's core issue: `savedTotalUnderlying` was decremented by the requested withdrawal amount, not the amount `withdrawFromProtocol()` actually realized, and was never reconciled afterward. Here, the StackerDB inventory is advanced by the requested/pushed version, not the version actually committed by `try_replace_chunk`, and is never reconciled if the deferred write fails or is dropped.

### Impact Explanation
Any remote, unprivileged peer that can open a p2p connection can push a `StackerDBPushChunk` for a slot it (validly) owns, or force races on shared slots. A node receiving that push will send back an inventory reply claiming to hold the new chunk version before it is durably stored, and (per the documented drop path) may never store it at all. Downstream neighbors that rely on `StackerDBChunkInv` to decide what is "rarest"/"already replicated" (as described in the sync module docs) will treat this node as having the latest version and will not fetch it from elsewhere, while the node itself does not actually have the data. This is a form of serving non-canonical StackerDB state as canonical and can steer StackerDB sync peers away from the actual chunk holder, degrading/breaking propagation of signer messages that ride on StackerDB (e.g. signer state-machine updates, block responses) — matching the "High: serving non-canonical state as canonical, steering a node off the tip via false inventory" category.

### Likelihood Explanation
This requires no privileged capability — any connected p2p peer can send `StackerDBPushChunk` messages for slots whose signer key it controls (which is by design open/permissionless for the slot's designated signer, but any third party can also race pushes for the same slot from different senders/paths to trigger the failure branch, or simply rely on the documented view-mismatch drop). The bug is deterministic given the described race/view-change condition, and the project's own tests already demonstrate the "accepted push not actually stored" scenario, indicating this is a routinely reachable code path, not a theoretical corner case.

### Recommendation
Do not patch the `StackerDBChunkInv` reply (or send an accepting reply) until the chunk has actually been durably committed via `try_replace_chunk`. Either:
1. Move the `try_replace_chunk` call (and its success/failure result) synchronously into `handle_unsolicited_StackerDBPushChunk` before constructing/sending the inventory reply, patching `slot_versions` only on confirmed success; or
2. If asynchronous storage must be kept for performance, only reply/advertise the new version after `process_stacker_db_chunks` confirms the write, and never advance the in-flight `StackerDBChunkInv` reply optimistically based on the "requested" version.

### Proof of Concept
1. Peer A opens a p2p connection to Node N and StackerDB peer sync/config is set up for contract `C` with slot 0 owned by A's key.
2. Peer A sends `StackerDBPushChunk{contract_id: C, slot_id: 0, slot_version: v+1, rc_consensus_hash: H}` signed correctly.
3. `handle_unsolicited_StackerDBPushChunk` runs `validate_received_chunk` (passes: correct signer, version, size), patches `data.slot_versions[0] = v+1`, and immediately sends back a signed `StackerDBChunkInv` to A (and this reply is also what N would serve to any other peer inventory requester) — as in `stackslib/src/net/stackerdb/mod.rs:794-807,858-870`.
4. Before N's relayer processes the forwarded chunk in `process_stacker_db_chunks`, either (a) N's chain view moves on so `rc_consensus_hash` no longer matches (as reproduced by the existing test at `stackslib/src/net/tests/mod.rs:1612-1624`, which explicitly notes the chunk "gets dropped ... and we never stored it"), or (b) a concurrently-processed chunk for the same slot is committed first, making the deferred `try_replace_chunk(&sc, &md, &chunk.data)` in `relay.rs:2412` return `Error::StaleChunk`, hitting the `continue` branch at `relay.rs:2434` with no correction to any already-sent inventory.
5. Node N's on-disk StackerDB for slot 0 still reflects version `v` (or a different chunk than advertised), but N had already advertised version `v+1` as accepted to peer A and to any inventory requester who received/observed that state — a durable mismatch between advertised and actual StackerDB state until the next legitimate write to that slot.

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

**File:** stackslib/src/net/tests/mod.rs (L1618-1624)
```rust
    // n1's *pushed* stackerdb chunk gets dropped since its rc_consensus_hash no longer matches:
    // a peer sent it to us under a view we have moved past, and we never stored it.
    //
    // n1's *uploaded* stackerdb chunk is kept.
    network_result_union
        .uploaded_stackerdb_chunks
        .append(&mut n1.uploaded_stackerdb_chunks);
```
