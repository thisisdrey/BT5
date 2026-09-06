### Title
Node ACKs/advertises a StackerDB chunk version it has not yet persisted, allowing inventory to diverge from actual stored state - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
In `PeerNetwork::handle_unsolicited_StackerDBPushChunk`, the function validates an unsolicited pushed chunk with `validate_received_chunk` (an authentication/freshness check only) and then immediately patches and replies with a `StackerDBChunkInv` claiming the new `slot_version` is now held locally — before the chunk is ever written to the StackerDB database.

### Finding Description
`validate_received_chunk` at [1](#0-0)  only checks chunk size, expected-version-or-newer, signer authenticity, and max-write count. It explicitly does **not** perform the actual write, and its own doc comment notes it "does not check write frequency, since the caller has different ways of doing this."

In `handle_unsolicited_StackerDBPushChunk`, once `validate_received_chunk` returns `true`, the code directly patches the in-memory `StackerDBChunkInvData` to reflect the new version and unconditionally sends this as a signed reply to the peer: [2](#0-1) [3](#0-2) 

The actual persistence of the chunk — via `StackerDBTx::try_replace_chunk`, which enforces a *strictly greater* version check (`slot_desc.slot_version <= slot_validation.version` → `StaleChunk`) and `max_writes` — only happens later, asynchronously, on the relayer thread through `Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks` → `tx.try_replace_chunk`: [4](#0-3) [5](#0-4) 

This creates a gap between "authenticated and equal-or-newer version" (checked immediately, in `validate_received_chunk`) and "actually stored" (checked later, strictly, in `try_replace_chunk`). Because the p2p thread commits to the claimed version in its outbound `StackerDBChunkInvData` before the relayer thread has attempted (or succeeded at) the real database write, the advertised inventory can be wrong: a race between two nearly-simultaneous pushes of the same/adjacent version, a later `StaleChunk`/`TooManySlotWrites` rejection on the relayer thread, or a crash/restart between the ACK and the deferred write, all leave the node asserting (via its immediate reply, and via subsequent `StackerDBChunkInv` served through `make_StackerDBChunksInv_or_Nack`, which reads live DB state) an inconsistent view of what it actually holds.

### Impact Explanation
This breaks the "served vs. stored/committed" equality the rest of the StackerDB protocol depends on for correctness of rarest-first replication (`make_chunk_request_schedule`, `make_chunk_push_schedule` in `stackslib/src/net/stackerdb/sync.rs`). Peers that see this node's advertised inventory as authoritative may skip requesting the chunk from other replicas (believing it is already replicated here), yet a subsequent `StackerDBGetChunk` to this node can return the old chunk or a NACK, since the DB was never actually updated to that version. This is a High-severity issue under the rubric's "serving non-canonical state as canonical" category, since a remote unprivileged peer can trigger the immediate, unconditioned ACK simply by pushing an unsolicited chunk.

### Likelihood Explanation
Reachable by any connected p2p peer sending an unsolicited `StackerDBPushChunk` message with a validly-signed chunk (the signer key requirement is on the chunk's slot owner, not the sending peer — any peer can relay/forward a validly-signed chunk they've observed). The race window (reply sent on p2p thread vs. store on relayer thread) is a normal, always-present part of the message flow, not a rare corner case, though the resulting inventory/DB mismatch is transient and self-heals once the relayer thread completes the store or on the next sync round.

### Recommendation
Do not patch/advertise the updated `slot_version` in the immediate `StackerDBChunkInvData` reply until the chunk has actually been durably written (e.g., synchronously call `try_replace_chunk` before constructing the ACK/inv reply, or defer the reply until the relayer thread confirms storage), so that the advertised inventory always reflects true on-disk state.

### Proof of Concept
Not independently verified end-to-end (would require running two peers and racing writes/reads); the code-path analysis above is based on static tracing of `handle_unsolicited_StackerDBPushChunk` (mod.rs:742-871), `validate_received_chunk` (mod.rs:649-717), `Relayer::process_stacker_db_chunks`/`process_pushed_stacker_db_chunks` (relay.rs:2385-2493), and `StackerDBTx::try_replace_chunk` (db.rs:398-438), showing the ACK-before-store ordering and the looser (`>=`) vs. stricter (`>`) version-check discrepancy between the two paths.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
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
```

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
