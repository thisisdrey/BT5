### Title
Code asymmetry between `validate_received_chunk` and `try_replace_chunk` causes a node to serve inventory (`StackerDBChunkInv`) claiming a chunk version as accepted when the DB layer will reject it as stale - (File: stackslib/src/net/stackerdb/mod.rs, stackslib/src/net/stackerdb/db.rs)

### Summary
`PeerNetwork::validate_received_chunk` and `StackerDBTx::try_replace_chunk` enforce the *same* logical invariant — whether an incoming chunk's `slot_version` is fresh enough to accept — using two different boundary conditions (`<` vs `<=`). This mirrors the reported bug class: one function treats a boundary value (`slot_version == current/expected version`) as acceptable/fresh, while the sibling function that actually commits the write treats that same value as stale and rejects it. A remote, unprivileged peer can trigger the divergence by relaying an already-seen, validly-signed `StackerDBPushChunk` at the currently-stored version, causing the receiving node to optimistically patch and sign a `StackerDBChunkInv` reply that claims the chunk as accepted before the DB layer runs, while `try_replace_chunk` never actually commits (or updates) that state.

### Finding Description
In `validate_received_chunk`, freshness is checked with a strict less-than: [1](#0-0) 

This means a chunk whose `slot_version` **equals** the node's currently expected/stored version passes validation as "not stale."

In `StackerDBTx::try_replace_chunk`, the DB-layer freshness check uses `<=`, so a chunk at the same version as what is already stored is explicitly rejected as `StaleChunk`: [2](#0-1) 

`handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk` first, and upon success it **patches its own outgoing inventory reply** (`data.slot_versions[slot_id]`) to the incoming chunk's version *before* the chunk is actually committed via `try_replace_chunk`: [3](#0-2) 

That patched `StackerDBChunkInv` payload is then signed and sent back to the peer as the node's authoritative inventory for this contract, and the function separately signals `(false, true)` to forward the chunk to the relayer for actual storage: [4](#0-3) 

The relayer eventually calls `try_replace_chunk` via `process_stacker_db_chunks`, which — for the boundary case where `slot_version == expected_version == slot_validation.version` — rejects it as `StaleChunk` and never writes to storage: [5](#0-4) [6](#0-5) 

The net effect: the node signs and transmits an inventory message asserting it has processed/updated a given slot to a specific version, while the on-disk `chunks` table's `version`/`write_time` were never touched for that event. This is a "served vs. committed" equality break — the exact class of asymmetry described in the external report (one code path permits a boundary condition, a sibling path that performs the actual state mutation rejects the identical boundary condition), just manifesting in inventory/version bookkeeping instead of a financial total.

### Impact Explanation
This does not directly corrupt chunk *data* (the chunk's `data_hash` is always derived from the actual `data` bytes at every call site, so an attacker cannot forge mismatched content this way), but it does let an unauthenticated relay-only participant (no need to possess the slot's private key) cause a victim node to emit a **signed** p2p `StackerDBChunkInv` reply whose contents do not reflect the local canonical DB state (`chunks.version`). Under the taxonomy in scope, this falls under "serving non-canonical state as canonical" (High-tier impact category), since a downstream peer or sync state machine consulting this node's advertised inventory is told a slot is at a version that this replica has not actually recorded/committed for that push event.

### Likelihood Explanation
Likelihood is constrained: the divergence only manifests exactly at the boundary case `slot_version == expected_version == currently-stored version` (i.e., a legitimately-signed chunk being replayed at the node's already-current version) — a case that in practice is a re-send of already-known data rather than new or forged content. It requires no privileged key, only that the attacker relay a previously observed, validly signed chunk (any unprivileged peer can do this), so the trigger condition itself is trivially reachable remotely, but the "damage" is bookkeeping/inventory inconsistency rather than corruption of served bytes.

### Recommendation
Make the staleness/freshness boundary condition identical in both `validate_received_chunk` (`stackslib/src/net/stackerdb/mod.rs:700`) and `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:424`) — both should use the same operator (`<` or `<=`) when comparing `slot_version` to the current/expected version. Additionally, avoid patching the outgoing `StackerDBChunkInv` reply's `slot_versions` optimistically before the chunk has actually been committed via `try_replace_chunk`; the inventory patch should be derived from the DB's actual post-write state (or the write result) rather than from the pre-write validation outcome.

### Proof of Concept
1. Node V (victim) stores a StackerDB chunk for `(contract_id, slot_id=0)` at `version = N`, signed by the slot's legitimate owner (`slot_validation.version == N`).
2. Attacker A (any connected, unprivileged peer, does not need the slot's private key) captures this valid, signed chunk (e.g., by observing gossip) and sends it back to V unsolicited as a `StackerDBPushChunk` with `slot_version = N`.
3. V's `handle_unsolicited_StackerDBPushChunk` calls `validate_received_chunk`, which passes because `data.slot_version (N) < *expected_version (N)` is false → treated as fresh (`stackslib/src/net/stackerdb/mod.rs:700`).
4. V patches its outgoing `StackerDBChunkInv.slot_versions[0] = N` (already true, but the code path treats this as a successful acceptance) and signs+sends this inventory reply to A (`stackslib/src/net/stackerdb/mod.rs:807, 858-871`); simultaneously the chunk is forwarded to the relayer for storage.
5. In the relayer, `try_replace_chunk` evaluates `slot_desc.slot_version (N) <= slot_validation.version (N)` → true → returns `StaleChunk`, so no DB write/`write_time` update occurs (`stackslib/src/net/stackerdb/db.rs:424-429`).
6. The result: V has sent a signed `StackerDBChunkInv` reply implying successful chunk processing at version N for this push event, while the corresponding storage-layer commit never happened for this event — the served inventory state and the actually-committed DB state have diverged in provenance, even though in this specific boundary case the final version number coincidentally matches.

Note: I was not able to fully verify whether any downstream consumer (e.g., the StackerDB sync scheduler on a different node) would meaningfully misbehave from this specific coincidental-version case, since the visible `slot_versions` value is unchanged from before the event. A stronger, more clearly damaging variant of this asymmetry (e.g., one where the inventory is patched to a version *strictly greater* than what gets committed) was not found in the paths inspected; confirming there is no such variant elsewhere in `stackslib/src/net/stackerdb/**` would require further review that exceeded the available iterations.

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

**File:** stackslib/src/net/stackerdb/mod.rs (L784-815)
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

**File:** stackslib/src/net/stackerdb/db.rs (L400-429)
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
```

**File:** stackslib/src/net/relay.rs (L2408-2437)
```rust
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
