## Title
Node acknowledges receipt of a pushed StackerDB chunk (and advertises the new slot version) before the chunk is actually persisted - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` computes the reply `StackerDBChunkInv` by taking the *current on-disk* inventory from `make_StackerDBChunksInv_or_Nack`, validating the pushed chunk with `validate_received_chunk`, and then directly patching the in-memory inventory it is about to send back to the peer (`*slot_version = chunk_data.chunk_data.slot_version;`) — all without ever calling into `StackerDBTx::try_replace_chunk`/`insert_chunk` to actually write the chunk to the database in this function. [1](#0-0) 

### Finding Description
The comment right before the reply is sent says: *"this is a reply to the pushed chunk, and we can store it right now (so don't buffer it)"*, implying storage has happened. However, within `handle_unsolicited_StackerDBPushChunk` itself there is no call to the StackerDB storage layer (`try_replace_chunk`/`insert_chunk`, see `stackslib/src/net/stackerdb/db.rs:398-438`) — the function only mutates the reply's `slot_versions` vector in RAM and signs/sends that reply: [2](#0-1) 

Actual persistence of pushed chunks is deferred to the relayer via the `(bool, bool)` return value — the second boolean signals "forward to the relayer for processing," and it is that separate downstream pipeline (feeding into `StackerDBSyncResult::from_pushed_chunk` / `chunks_to_store`, in `stackslib/src/net/stackerdb/mod.rs:520-535`) that must independently persist the chunk. Yet the P2P reply — which reports our own inventory (`slot_versions`) as authoritative and is what neighbors use to decide what to fetch/re-push (see `make_chunk_request_schedule`/`make_chunk_push_schedule` in `sync.rs`) — has already been sent out claiming the new version is present.

This breaks the equality "advertised inventory == what is actually committed to the local StackerDB." If the deferred store in the relayer path fails, is skipped, or races with a concurrent stale/overwritten chunk, the node has already told the network (via the signed `StackerDBChunkInv` reply) that it holds the new `slot_version`, even though the chunks table on disk was never updated by this code path.

### Impact Explanation
Downstream peers rely on the received `StackerDBChunkInv` to decide which slots are "caught up" and to stop requesting/pushing that slot version (`make_chunk_request_schedule` compares `local_version >= remote_version` and skips fetching, `sync.rs:365-368`). A node that falsely advertises a version it doesn't actually hold can cause that chunk version to silently disappear from the mesh's effective replication (no peer will re-fetch/re-push it to this node once it believes the node already has it), which is a form of serving non-canonical/false inventory as canonical to the P2P network — matching the "steering a node off the tip via false inventory" impact class for StackerDB replicas (e.g., signer message sets, miner-coordination DBs).

### Likelihood Explanation
This is reachable by any unprivileged remote peer simply by sending an unsolicited, validly-signed `StackerDBPushChunk` (the normal, expected write path for StackerDB — no special privilege required beyond already being an authorized slot signer for that DB, which is the same requirement the protocol already expects for any legitimate write). The race/failure window between "reply says we have it" and "we actually write it" depends on whatever failure modes exist in the relayer's deferred-store path, which I could not fully trace given the remaining iteration budget — I was unable to confirm from the fetched context whether the relayer's store step is guaranteed to succeed whenever this function returns `(false, true)`, or whether any legitimate failure/rejection can occur there (e.g. concurrent overwrite, stale-by-then check, DB error) that would make the previously-sent ack false. This uncertainty affects confidence in real-world exploitability, though the code-level equality violation (ack-before-write) is clearly present.

### Recommendation
Do not patch/send the `slot_version` in the outgoing inventory reply until after the chunk has been durably written via `StackerDBTx::try_replace_chunk`. Either move the actual store call into `handle_unsolicited_StackerDBPushChunk` before constructing the reply, or only report the new slot version in `data.slot_versions` after receiving confirmation from the relayer that the chunk was actually persisted.

### Proof of Concept
Not independently reproduced with a running two-node harness within the available tool budget; the analysis is based on direct code reading of `stackslib/src/net/stackerdb/mod.rs:742-871`, which shows the patch of `slot_version` and the signed reply happening with no interleaved call to any chunk-storage function. A concrete PoC would require instrumenting/pausing the relayer's chunk-store step (or injecting a store failure) while observing the P2P `StackerDBChunkInv` reply already claiming the new version — this exact interaction with `relay.rs`'s deferred processing could not be fully confirmed in this session.

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
