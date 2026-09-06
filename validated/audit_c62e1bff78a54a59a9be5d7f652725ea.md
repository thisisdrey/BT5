### Title
Advertised StackerDB inventory is patched to claim a pushed chunk's version before it is actually committed, allowing a false inventory to be served to peers - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an unsolicited `StackerDBPushChunk` message and then immediately patches the local `StackerDBChunkInv` reply's `slot_versions` entry to the *pushed* chunk's version before that chunk has actually been written to the local `StackerDBs` store. The reply is sent back to the peer as if the new version is already stored, but the real write only happens later, asynchronously, via the relayer (`process_stacker_db_chunks` → `try_replace_chunk`), which can still fail (e.g. `StaleChunk` due to a race with a concurrently-processed higher-version chunk, or a DB error). This breaks the equality between "advertised/served inventory state" and "actually committed StackerDB state."

### Finding Description
In `handle_unsolicited_StackerDBPushChunk`, once `validate_received_chunk` succeeds (signature, size, freshness, max-writes checks against the *currently known* `data.slot_versions`), the code does: [1](#0-0) 
which unconditionally sets the in-memory `slot_version` in the outgoing `StackerDBChunkInvData` payload to the pushed chunk's `slot_version`, before any call to `try_replace_chunk` occurs in this function. The function then returns `Ok((false, true))`, meaning "don't buffer, but forward to the relayer" - the actual persistence of the chunk happens later in a completely separate code path, `PeerNetwork::process_stacker_db_chunks`, which performs the real, authoritative write via `try_replace_chunk`: [2](#0-1) 
`try_replace_chunk` re-validates staleness/signer/max-writes against the database state at the time it actually executes: [3](#0-2) 
Because the `StackerDBChunkInv` reply is sent to the peer immediately (before the relayer runs), and because the relayer's later `try_replace_chunk` call can independently fail (most notably `StaleChunk` if a different, higher-version chunk for the same slot is processed first, since the two code paths race against the same slot with no shared lock at the time the inventory is patched), the node can advertise having a chunk version in its `StackerDBChunkInv` that it never actually stores. `validate_received_chunk`'s docstring explicitly acknowledges the write-frequency gate is *not* enforced here ("NOTE: does not check write frequency, since the caller has different ways of doing this"), reinforcing that this function's optimistic inventory patch is not the same gate as the actual commit path.

### Impact Explanation
A node's `StackerDBChunkInv` inventory is the mechanism peers use to decide what to download and from whom ("prioritizing them by newest-first" per the sync docs). If a node's advertised inventory claims a slot version that it does not actually hold, peers that rely on this inventory to schedule chunk downloads will request that specific version from this node and get a stale/absent chunk (`NoSuchSlot`/an older version), steering their downloads based on false inventory data. This matches the "steering a node off the tip via false inventory" impact class (High) called out in the rules, since StackerDB inventory here plays the analogous role of chain-tip inventory for chunk propagation.

### Likelihood Explanation
Triggering this requires only sending two (or more) valid, properly-signed `StackerDBPushChunk` messages for the same slot with different versions to a victim node in quick succession from any unprivileged peer connection - no special access, secret keys of others, or admin role is needed, only a valid slot-owner signature for the attacker's own slot, which any registered signer already possesses. Because `handle_unsolicited_StackerDBPushChunk` immediately replies with the patched inventory while the actual commit is deferred and re-validated independently (and can race/fail), the mismatch window exists on every push, not merely under contrived conditions, though it is more easily forced deterministically by racing two versions for the same slot.

### Recommendation
Do not patch and send back an updated `StackerDBChunkInvData` slot version until the chunk has actually been durably stored (i.e., only patch the inventory that is sent in reply to a push after `try_replace_chunk` succeeds — or, if deferring persistence to the relayer, defer the inventory reply as well and only advertise an updated version once the relayer's replace has actually committed). Alternatively, perform a synchronous store attempt in `handle_unsolicited_StackerDBPushChunk` itself and reflect the true outcome of `try_replace_chunk` in the returned inventory patch, so the advertised state always matches the durable store state.

### Proof of Concept
1. Attacker controls two valid signing keys/slots is not required — a single valid slot owner is enough to demonstrate the race: have the attacker (owner of slot `S`) push `StackerDBPushChunk` for slot `S` version `v1`, then immediately push a second `StackerDBPushChunk` for slot `S` with version `v2 > v1` on a second connection/event before the relayer has processed the first.
2. Both pass `validate_received_chunk` against the same currently-known `expected_versions` (since neither has been committed to the DB yet), so both patch their respective in-memory `StackerDBChunkInvData` and are replied to the peer as accepted, and both are forwarded to the relayer.
3. In `process_stacker_db_chunks` -> `try_replace_chunk` ( [2](#0-1) ), whichever push is processed second and has a lower slot_version than the (now committed) other is now rejected with `StaleChunk` and simply dropped/logged, never re-notifying the peer that the earlier positive inventory reply was inaccurate.
4. Any peer that already received a `StackerDBChunkInvData` claiming version `v1` (from step 1's immediate reply, before its subsequent `StaleChunk` rejection) will later request that exact version from this node and fail to receive it, having scheduled its download based on inventory that never materialized in the store.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L794-807)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L400-437)
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
```
