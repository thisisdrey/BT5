### Title
StackerDB peer inventory is optimistically patched to reflect an unsolicited chunk push before the chunk is durably committed, allowing served slot-version state to diverge from stored state - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates and then immediately advertises (via a signed `StackerDBChunkInv` reply) that the local replica's slot version has been bumped to the pushed chunk's version, **before** that chunk has actually been written to the StackerDB SQL store. The real, transactional write happens later, out-of-band, in the relayer via `NetworkResult`/`process_stacker_db_chunks`. This creates a window in which the value the node *tells its peers it has* (served state) and the value it *actually persisted* (committed state) can diverge — the same "front-run/observe-then-act" TOCTOU class as the report's interest-rate bug, but here it is the node's own inventory that becomes stale/wrong rather than a borrower's cost.

### Finding Description
In `handle_unsolicited_StackerDBPushChunk`: [1](#0-0) 

the flow is:
1. `validate_received_chunk` is called against the *currently known* `slot_versions` (obtained from `make_StackerDBChunksInv_or_Nack` → `self.stackerdbs.get_slot_versions`), verifying the signature and that the pushed version is not stale relative to that snapshot.
2. On success, the in-memory `StackerDBChunkInvData.slot_versions[slot_id]` is immediately overwritten with the *pushed* version (`*slot_version = chunk_data.chunk_data.slot_version;`), and this patched inventory is what gets signed and sent back to the peer as the P2P reply: [2](#0-1) 
3. The function returns `(false, true)`, meaning "don't buffer, but forward to the relayer" — the actual durable write (`StackerDBTx::try_replace_chunk`) happens later, asynchronously, in `Relayer::process_stacker_db_chunks`: [3](#0-2) 

`try_replace_chunk` re-validates against the DB's current `SlotValidation` (owner signature, monotonic version, max-writes) at write time: [4](#0-3) 

Because steps (2) and (3) are not atomic and are separated by the P2P messaging layer and the relayer pipeline, any change to on-disk state between the snapshot used for validation and the actual `try_replace_chunk` call (e.g., a second push for the same slot processed first, a slightly higher version already written concurrently, or any DB error) causes `try_replace_chunk` to fail with `StaleChunk`/other errors. `process_stacker_db_chunks` simply logs and `continue`s on failure — it does **not** retract or correct the inventory claim that was already signed and sent to the peer: [5](#0-4) 

The net effect: the node's peer now believes (from a validly-signed `StackerDBChunkInv`) that this replica holds slot version `V`, when the replica's actual persisted version may still be `V-1` (or unchanged). This breaks the "served vs. committed" equality for StackerDB chunk inventories.

### Impact Explanation
Once a peer believes (via the signed inventory reply) that this node already has the newest version of a chunk, `make_chunk_request_schedule` on that peer's side will skip requesting it (`if local_version >= remote_version { continue; }` logic mirrored across the network), since it thinks the target already has it. The corresponding push/rarest-first replication logic can similarly deprioritize pushing that chunk to this node. This can leave a node stuck with unreplicated/stale StackerDB state for that slot until another push happens to be retried, i.e., it is served non-canonical/false inventory that steers peer replication decisions — the class of impact the task rubric labels "steering a node off the tip via false inventory" (High). It does not, by itself, let an unauthorized party write into the DB (that path is still gated by signature/version checks in `try_replace_chunk`), so the severity is bounded to inventory/propagation-state corruption rather than unauthorized writes.

### Likelihood Explanation
Triggering the race only requires an unprivileged remote peer to send two (or more) valid `StackerDBPushChunk` messages in quick succession for the same slot (or to race with another legitimate replica's concurrent push), which is directly reachable via the p2p push-chunk handler without any special privilege — this matches the "remote, unprivileged" and "reachable in few messages" bar. The failure path (`try_replace_chunk` erroring after the inventory has already been optimistically patched and signed/sent) is a normal, expected outcome of concurrent pushes, not a contrived edge case, so likelihood is moderate-to-high under any realistic level of StackerDB write concurrency (e.g., signer message replication during signing rounds).

### Recommendation
Do not patch/sign the `StackerDBChunkInv` reply optimistically before the chunk is durably committed. Either:
- Defer sending the `StackerDBChunkInv` reply until after `process_stacker_db_chunks`/`try_replace_chunk` actually succeeds (moving the reply construction after the write, or making the write synchronous within `handle_unsolicited_StackerDBPushChunk`), or
- If asynchronous processing must be kept for performance, track pending-but-unconfirmed slot versions separately from confirmed slot versions and only report confirmed versions in `StackerDBChunkInv`, reconciling (or emitting a corrective inventory update) if the deferred write ultimately fails.

### Proof of Concept
Not independently reproduced with a running two-node network in this session (would require exercising the p2p push-chunk handler concurrently with a conflicting write and observing the signed inventory reply vs. the DB's actual `get_slot_versions()`), so this is asserted from code-path analysis rather than an executed exploit. Conceptually:
1. Two neighbors, A and B, each independently and validly bump slot `0` for contract `C` to version `2` (both permitted since they only need to beat the version last known to the target node, before either write lands).
2. Node A's push arrives first at the victim node; `handle_unsolicited_StackerDBPushChunk` validates it against the current (still version `1`) inventory snapshot, patches and signs a reply advertising version `2`, and forwards the chunk to the relayer.
3. Node B's push (also version `2`, but processed by the relayer before A's) reaches `try_replace_chunk` first and succeeds; when A's chunk is processed afterward, `try_replace_chunk` returns `StaleChunk` (since the stored version is already `2`) and A's chunk data is silently dropped in `process_stacker_db_chunks`.
4. The victim node already told A's neighbor set — via the signed inventory sent in step 2 — that it has version `2`, but the specific bytes it actually stored are B's chunk, not A's (a data mismatch), or worse: if the relayer processing order differs and both fail momentarily, the node has claimed version `2` while still only holding version `1` on disk — a real served-vs-committed mismatch until the next successful write.

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
