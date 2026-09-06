### Title
False StackerDB inventory acknowledgment before chunk is actually stored — advertised-vs-stored state mismatch - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates an unsolicited push and immediately replies to the sender with a `StackerDBChunkInv` claiming the new slot version is now held locally — by patching the in-memory `slot_versions` reply vector — before the chunk is ever written to the StackerDB (`try_replace_chunk`). Actual persistence only happens later and asynchronously via `Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks`. This breaks the equality between "advertised inventory" and "actually stored data," directly analogous to the stNXM `extendDeposit()` bug where the tracking mapping was updated to reflect a state transition that had not (yet, or ever) actually completed.

### Finding Description
In `stackslib/src/net/stackerdb/mod.rs`, `handle_unsolicited_StackerDBPushChunk` (around lines 742–871):
1. It builds a `StackerDBChunkInv` reply via `make_StackerDBChunksInv_or_Nack`, which freshly loads `slot_versions` from the DB (`self.stackerdbs.get_slot_versions`) — i.e., the *true* current state. [1](#0-0) 
2. After `validate_received_chunk` succeeds (signature check, version freshness, `max_writes` bound — but explicitly **not** `write_freq`, per the doc comment), the code directly patches the just-loaded `slot_versions` entry to the *new, unwritten* version and sends this as the acknowledgment to the peer: [2](#0-1) 
3. The chunk itself is **not** stored in this code path. It is merely forwarded to the relayer (`Ok((false, true))`), and only actually persisted later in `Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks`, which calls `tx.try_replace_chunk(...)`: [3](#0-2) 

Because `try_replace_chunk` performs its own authoritative checks (stale version, `TooManySlotWrites`, and crucially **write-frequency** enforcement not checked in `validate_received_chunk`), storage can still fail or be rejected at that later point, even though the immediate P2P reply already told the sender/peer "I now have version N." This produces a genuine, remotely-reachable divergence between the locally advertised inventory (sent out over the wire to another node) and the actual persisted chunk state — the same class of bug as `stNXM`'s stale `tokenIdToTranches` mapping causing `stakedNxm()`/`totalAssets()` to misreport real state after a completed-but-untracked transition.

### Impact Explanation
This is a remote, unauthenticated-adjacent (any connected P2P neighbor can push a chunk) equality break between served/advertised state and actually-committed state:
- A neighbor that receives this optimistic ack believes the local node already has version N of the chunk and may stop retransmitting or de-prioritize re-pushing it, while the local replica's real slot version (as queried by any *other* peer via a fresh `StackerDBGetChunksInv`) is still the old, stale one until/unless the deferred `try_replace_chunk` succeeds.
- If the deferred store is rejected (e.g., write-frequency throttling, which is intentionally skipped in `validate_received_chunk` per its own doc comment, or any other later-occurring staleness), the node has now propagated inventory information to the network that does not match reality, without ever correcting it back to the peer that was told the wrong thing (no negative follow-up ack is sent).
- This matches the "serving non-canonical state as canonical" / false-inventory pattern called out as in-scope High impact, though the blast radius here is narrower than a network-wide false broadcast: the false ack is sent only to the single pushing peer, and legitimate `StackerDBGetChunksInv` queries from other peers still return the DB's true (re-queried) versions, so the misinformation is not durably or widely propagated. This bounds the severity below "network-wide propagation of forged data" and is best characterized as a transient, self-limiting inventory-accounting inconsistency — directly comparable in shape and severity to the underlying Medium-rated stNXM report (temporary, self-correcting mismatch between tracked and actual state).

### Likelihood Explanation
Reaching this code path only requires being a connected P2P neighbor sending an unsolicited, validly-signed `StackerDBPushChunk` at a version the local node doesn't yet have — a normal, permissionless, unprivileged interaction that every StackerDB-replicating node must accept from any neighbor. No special timing race beyond ordinary asynchronous processing (deferred storage) is needed to reach the inconsistent state; a failure/rejection at the deferred `try_replace_chunk` step (e.g., due to write-frequency limits deliberately not checked earlier) is a normal occurrence in the design, so the divergence window is easily and routinely triggerable, not merely theoretical.

### Recommendation
Do not report the new slot version as accepted in the immediate `StackerDBChunkInv` reply until the chunk has actually been durably stored (i.e., move the ack generation to occur after — or gate it on the success of — the corresponding `try_replace_chunk` call), or alternatively perform the store synchronously as part of `handle_unsolicited_StackerDBPushChunk` before constructing the reply, so the acknowledgment always reflects the durable, authoritative on-disk state rather than an assumed future state.

### Proof of Concept
Not independently reproduced (no test execution available); the flow is demonstrated purely via static code inspection:
1. Peer B connects to Node A and sends a validly-signed `StackerDBPushChunk` for slot 0 at version N (newer than A's version N-1), with `config.write_freq` set high enough that repeated recent writes to slot 0 would violate the frequency limit (this check is deliberately skipped by `validate_received_chunk`). [4](#0-3) 
2. Node A's `handle_unsolicited_StackerDBPushChunk` validates the push, patches its reply `slot_versions[0] = N`, and immediately sends this `StackerDBChunkInv` back to Peer B — telling B "I now have version N." [2](#0-1) 
3. The chunk is queued to the relayer; later, `process_pushed_stacker_db_chunks` → `process_stacker_db_chunks` calls `tx.try_replace_chunk`, which independently enforces write-frequency and can reject the write, leaving A's DB at version N-1. [3](#0-2) 
4. Peer B, trusting the earlier ack, stops re-pushing/re-serving slot 0's chunk to A, while any third peer C querying A via `StackerDBGetChunksInv` at that same moment would see A still at version N-1 (since `make_StackerDBChunksInv_or_Nack` re-queries the DB fresh) — demonstrating the advertised-vs-stored inconsistency.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
    }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L761-792)
```rust
        let mut payload = self.make_StackerDBChunksInv_or_Nack(
            naddr,
            chainstate,
            &chunk_data.contract_id,
            &chunk_data.rc_consensus_hash,
        );
        match payload {
            StacksMessageType::StackerDBChunkInv(ref mut data) => {
                // this message corresponds to an existing DB, and comes from the same view of the
                // stacks chain tip
                let stackerdb_config = if let Some(config) =
                    self.get_stacker_db_configs().get(&chunk_data.contract_id)
                {
                    config
                } else {
                    // not for this DB
                    info!(
                        "StackerDBChunk for {} ID {} is not available locally",
                        &chunk_data.contract_id, chunk_data.chunk_data.slot_id
                    );
                    return Ok((false, false));
                };

                // sanity check
                if !self.validate_received_chunk(
                    &chunk_data.contract_id,
                    stackerdb_config,
                    &chunk_data.chunk_data,
                    &data.slot_versions,
                )? {
                    return Ok((false, false));
                }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L794-814)
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
