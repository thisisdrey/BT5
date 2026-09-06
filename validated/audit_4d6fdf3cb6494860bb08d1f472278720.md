### Title
StackerDB push-chunk validation is looser than the storage's version check, causing the node to advertise chunk versions it never actually committed - (File: `stackslib/src/net/stackerdb/mod.rs`, `stackslib/src/net/stackerdb/db.rs`)

### Summary
This is a direct analog of the Golom `[M-08]` bug class: a pre-check accepts a case that the underlying state-mutating operation will reject, so the code that trusts the pre-check's result diverges from what actually gets committed. In `PeerNetwork::validate_received_chunk`, a pushed chunk with `slot_version == expected_version` passes validation ("must be the current or newer version"), but the real storage routine `StackerDBs::try_replace_chunk` requires the new version to be *strictly greater* than the currently stored version, rejecting equal versions as `StaleChunk`. `handle_unsolicited_StackerDBPushChunk` uses the (looser) validation result to patch and advertise its local inventory as containing the new version, without ever calling `try_replace_chunk`/`insert_chunk` itself.

### Finding Description
`validate_received_chunk` treats `data.slot_version >= expected_version` as acceptable: [1](#0-0) 

But the actual DB write function enforces a *strictly greater* version, rejecting equality as stale: [2](#0-1) 

In `handle_unsolicited_StackerDBPushChunk`, when a push chunk arrives and the local view is fresh (`StackerDBChunkInv` branch), the code calls `validate_received_chunk` against the locally cached `data.slot_versions` and, if it returns `true`, directly overwrites the in-memory inventory entry to reflect the *pushed* version — it does **not** call `try_replace_chunk` here (actual storage happens later, asynchronously, in `PeerNetworkModule::process_stacker_db_chunks` in `relay.rs`, which does call `try_replace_chunk` and correctly drops `StaleChunk` errors): [3](#0-2) [4](#0-3) 

So the equality broken here is: **"chunk version accepted by the pre-check" != "chunk version actually persisted by the storage layer."** When a remote peer pushes a chunk at `slot_version == expected_version` (i.e., equal to what we already have on-disk) but with **different chunk bytes**, `validate_received_chunk` reports it as valid (since it only checks `>=`), and the node immediately patches and can advertise (via the `StackerDBChunksInv` reply built from the mutated `payload`) that it now holds that version — while the real store path (`relay.rs` → `try_replace_chunk`) will reject the write as `StaleChunk` because the version isn't strictly newer. The result is a local inventory/gossip state that is inconsistent with what is actually committed in the on-disk StackerDB (`insert_chunk`/`try_replace_chunk`): [5](#0-4) 

### Impact Explanation
This causes the node to serve/broadcast an inventory (`StackerDBChunkInv`) claiming it possesses a given chunk version when the underlying store never accepted that data, i.e., non-canonical/unwritten state advertised as canonical. Downstream effects:
- Peers that trust this inventory can request the chunk via `StackerDBGetChunk` and receive stale content that mismatches the version the inventory claimed, causing sync churn/NACKs, or wasted download/verification cycles across the network.
- Because `StackerDBChunkInv` responses are also used to decide whether to wake up sync state machines and schedule downloads/rebroadcasts (`stackerdb_sync.wakeup()`), this can cause spurious resync loops network-wide when triggered repeatedly by a malicious or buggy peer pushing same-version-different-data chunks.

This does not grant unauthorized writes (the actual authenticated write path in `try_replace_chunk` still enforces the true monotonic version and signature checks), so it does not reach "unauthenticated write to state." It best matches the High-tier category of "serving non-canonical state as canonical" via the inventory-advertising path.

### Likelihood Explanation
Any remote, unprivileged peer that is a valid signer for a slot (or simply a peer replaying a previously-valid, same-version chunk with altered bytes it captured/derived) can trigger this by pushing a `StackerDBPushChunk` whose `slot_version` equals the receiver's currently stored version. No special access or secret key beyond a valid slot signature is required to hit the mismatch itself — and even a signature-valid resend of the exact previous chunk at the same version will trip this every time, since `validate_received_chunk`'s freshness check is `>=` while storage's is `>`. This is a very cheap, deterministic trigger requiring only a couple of messages, not volumetric traffic.

### Recommendation
Align the freshness check in `validate_received_chunk` with the storage layer's semantics: require `data.slot_version > *expected_version` (matching `try_replace_chunk`'s `slot_desc.slot_version <= slot_validation.version` rejection), not `data.slot_version < *expected_version` as the only rejection condition. Additionally, `handle_unsolicited_StackerDBPushChunk` should not mutate/advertise the local inventory as containing the pushed version until the chunk has actually been durably written (or it should defer the inventory patch until after a successful `try_replace_chunk`/commit), to keep advertised inventory consistent with on-disk state.

### Proof of Concept
1. Node A is a signer for slot 0 of some StackerDB replica, with current stored `slot_version = 5`.
2. Attacker (holding slot 0's private key, or replaying a previously valid signed chunk) sends a `StackerDBPushChunk` message to node B with `slot_id = 0`, `slot_version = 5` (equal to B's `expected_versions[0]`), and arbitrary signed data.
3. In `handle_unsolicited_StackerDBPushChunk`, `validate_received_chunk` is invoked with `expected_versions = data.slot_versions` (B's current view); since `data.slot_version (5) < *expected_version (5)` is false, validation returns `Ok(true)`: [1](#0-0) 
4. The `StackerDBChunkInv` reply payload is patched to `slot_versions[0] = 5` (already the value, so no visible external difference in this exact case) and the message is forwarded to the relayer (`send_reply` / return `(false, true)`), which will eventually route it into `process_stacker_db_chunks` → `try_replace_chunk`, where `slot_desc.slot_version (5) <= slot_validation.version (5)` triggers `StaleChunk` and the chunk is silently dropped: [2](#0-1) [6](#0-5) 
5. Net effect: node B's sync state machine is woken up (`stackerdb_sync.wakeup()`), inventory bookkeeping churns, and the "accepted" chunk is never actually written — demonstrating that the pre-check (`validate_received_chunk`) and the true storage guard (`try_replace_chunk`) disagree on the boundary condition `slot_version == expected_version`, exactly mirroring the Golom finding's pattern of a pre-check accepting an input that the real state-mutation logic rejects.

**Note on completeness:** I could not fully trace whether the `data.slot_versions` patched in the `StackerDBChunkInv` reply is subsequently persisted anywhere beyond the immediate reply message, so the exact blast radius (e.g., whether other peers durably cache this false inventory) is not fully confirmed from the available code and would benefit from a live/dynamic trace (e.g., a Devin session) to confirm end-to-end propagation effects.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L699-706)
```rust
        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }
```

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

**File:** stackslib/src/net/stackerdb/db.rs (L371-396)
```rust
    /// Insert a chunk into the DB.
    /// It must be authenticated, and its lamport clock must be higher than the one that's already
    /// there.  These will not be checked.
    fn insert_chunk(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slot_desc: &SlotMetadata,
        chunk: &[u8],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "UPDATE chunks SET version = ?1, data_hash = ?2, signature = ?3, data = ?4, write_time = ?5 WHERE stackerdb_id = ?6 AND slot_id = ?7";
        let mut stmt = self.sql_tx.prepare(sql)?;

        let args = params![
            slot_desc.slot_version,
            Sha512Trunc256Sum::from_data(chunk),
            slot_desc.signature,
            chunk,
            u64_to_sql(get_epoch_time_secs())?,
            stackerdb_id,
            slot_desc.slot_id,
        ];

        stmt.execute(args)?;
        Ok(())
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L424-429)
```rust
        if slot_desc.slot_version <= slot_validation.version {
            return Err(net_error::StaleChunk {
                supplied_version: slot_desc.slot_version,
                latest_version: slot_validation.version,
            });
        }
```

**File:** stackslib/src/net/relay.rs (L2410-2437)
```rust
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
