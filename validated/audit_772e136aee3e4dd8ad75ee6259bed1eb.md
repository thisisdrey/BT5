### Title
Unsolicited StackerDB chunk push ack advertises a chunk version as stored before it is actually persisted, allowing false inventory to propagate - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` mutates and sends back a `StackerDBChunkInv` acknowledgment that claims the local replica now holds a given chunk version *before* that chunk is actually written to the StackerDB. The real, authoritative write happens later and separately, in the relayer path (`Relayer::process_stacker_db_chunks`), via `StackerDBTx::try_replace_chunk`. Because the ack is generated from mutated in-memory inventory rather than from confirmed storage, the value asserted to the remote peer can diverge from what actually ends up persisted, similar in spirit to the reported pattern of committing to external state before all internal state updates/checks have completed.

### Finding Description
In `handle_unsolicited_StackerDBPushChunk`, upon receiving a `StackerDBPushChunk`, the code builds an inventory/ack reply via `make_StackerDBChunksInv_or_Nack`, and when the reply is a `StackerDBChunkInv`, it runs `validate_received_chunk` (signature, freshness, size checks only — the function explicitly documents that it "does not check write frequency, since the caller has different ways of doing this") and then immediately patches the advertised slot version: [1](#0-0) 

This patched `data.slot_versions` is the payload signed and sent back to the peer as the node's inventory claim: [2](#0-1) 

At this point in `handle_unsolicited_StackerDBPushChunk`, the chunk has *not* been written to the StackerDB — no call to `StackerDBTx::try_replace_chunk` occurs in this function. The function only returns `(false, true)`, i.e. "forward to relayer," and the actual commit happens later in `Relayer::process_stacker_db_chunks`, which independently re-validates and calls `try_replace_chunk`: [3](#0-2) 

`try_replace_chunk` re-checks signer, staleness, and `max_writes`, and can fail (e.g. `StaleChunk` if another chunk for the same slot was processed/committed in the interim, or if the version is otherwise rejected) — in which case the earlier-sent ack is already wrong: the node told its peer it now has version `V`, but the DB was never updated to `V`. [4](#0-3) 

This breaks the equality between "served/advertised inventory" and "actually committed state": the wire ack is generated optimistically from the validation result rather than from a confirmed write, and the real state-changing effect (`try_replace_chunk` + `tx.commit()`) happens afterward, on a different code path, with no guarantee of consistency between the two.

### Impact Explanation
If the ack overstates what is actually stored, a remote peer that legitimately holds the chunk will believe this replica already has the latest version and will stop offering/pushing it. If the local `try_replace_chunk` in the relayer subsequently fails (stale-by-then, race with a concurrent write, or any other rejection), the chunk can be permanently missing from this replica until an unrelated future sync round detects the gap — i.e., the node can be steered off the current StackerDB "tip" via false inventory it itself advertised. This matches the specified High-severity category ("steering a node off the tip via false inventory").

### Likelihood Explanation
This requires only sending an unsolicited, validly-signed `StackerDBPushChunk` message that races with another concurrent write to the same slot (e.g., two neighbors pushing different chunk versions in close succession), a normal and remotely triggerable condition in a StackerDB replication network with no special privileges needed. The comment in the code itself acknowledges write-frequency and ordering are handled differently/loosely at this layer, indicating the maintainers were aware ordering guarantees are relaxed here.

### Recommendation
Do not patch/report an inventory slot version as updated (nor sign an ack claiming a chunk version is stored) until after the chunk has actually been committed to the StackerDB (i.e., after a successful `try_replace_chunk` + `tx.commit()`). Either perform the store synchronously before constructing the ack in `handle_unsolicited_StackerDBPushChunk`, or defer sending/patching the inventory ack until the relayer confirms the write succeeded.

### Proof of Concept
1. Two neighbors, A and B, both replicate the same StackerDB slot and push chunk versions `V1` (from A) and `V2` (from B) to node N in close succession, both validly signed.
2. N calls `handle_unsolicited_StackerDBPushChunk` for A's `V1` push: `validate_received_chunk` passes, `data.slot_versions[slot] = V1` is set, and N signs/sends a `StackerDBChunkInv` ack to A claiming slot version `V1` — before any DB write occurs.
3. Before the relayer commits A's chunk, N processes B's `V2` push through the same path and its relayer commit runs first, writing `V2` to the DB via `try_replace_chunk`.
4. When the relayer later attempts to commit A's `V1` chunk via `try_replace_chunk`, it now fails with `StaleChunk` (since stored version is already `V2` > `V1`) and the chunk is dropped, per ` [5](#0-4) `.
5. Node N has already told peer A (via the ack in step 2) that it holds version `V1`, but the DB never stores `V1`. A treats N as up to date for that version and will not re-push it, and if the network relies on that ack for propagation bookkeeping, `V1` can be lost from N's replica set despite the false confirmation.

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
