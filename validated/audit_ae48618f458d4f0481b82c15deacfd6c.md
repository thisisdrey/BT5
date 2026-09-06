### Title
`PeerNetwork::handle_unsolicited_StackerDBPushChunk` advertises a chunk as stored via the immediate `StackerDBChunkInv` reply before the chunk is actually committed, so peers can be told about state that does not exist - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
When a node receives an unsolicited `StackerDBPushChunk`, `PeerNetwork::handle_unsolicited_StackerDBPushChunk` validates the chunk (signature, staleness, size, max-writes) with `validate_received_chunk`, then immediately **patches the in-memory `StackerDBChunkInv` reply** to claim the new `slot_version` is now held, and sends that reply back to the peer right away. The actual database write only happens later and separately, when the relayer calls `process_pushed_stacker_db_chunks` → `process_stacker_db_chunks` → `StackerDBTx::try_replace_chunk`. The value announced as "accepted/committed" in the p2p inventory response is therefore recorded before, and independently of, whether the underlying `try_replace_chunk` write actually succeeds.

### Finding Description
`validate_received_chunk` (stackslib/src/net/stackerdb/mod.rs:649-718) explicitly does **not** check the same conditions enforced later by the real storage path in `StackerDBTx::try_replace_chunk` (stackslib/src/net/stackerdb/db.rs:400-438). The doc comment on `validate_received_chunk` states: "NOTE: does not check write frequency, since the caller has different ways of doing this." However, in the unsolicited-push handling path, that "different way" of checking write frequency (or any other storage-time failure condition, e.g. `NoSuchSlot` if the slot config changed between validation and storage) is never actually invoked before the reply is sent.

Concretely, in `handle_unsolicited_StackerDBPushChunk` (stackslib/src/net/stackerdb/mod.rs:742-871):
1. `validate_received_chunk` is called and only checks: chunk size, slot bounds, signature, staleness (`slot_version < expected_version`), and max_writes.
2. On success, the code directly does `*slot_version = chunk_data.chunk_data.slot_version;` in the `StackerDBChunkInvData` (line 807), which represents the node's claimed inventory of what it holds.
3. This patched inventory is immediately signed and sent back to the peer as the P2P reply (lines 862-870), *before* any DB write happens.
4. The actual write is deferred: the message is separately queued into `pushed_stackerdb_chunks` (stackslib/src/net/mod.rs:2249-2254) and only committed later via `Relayer::process_pushed_stacker_db_chunks` → `process_stacker_db_chunks` → `try_replace_chunk` (stackslib/src/net/relay.rs:2412, stackslib/src/net/stackerdb/db.rs:400-438).
5. `try_replace_chunk` performs its own independent checks — including `write_freq` throttling (not checked in `validate_received_chunk`) and slot existence — any of which can cause it to reject/skip the write (logged via `warn!("Failed to store chunk for StackerDB"...)` at relay.rs:2425-2433) with no correction sent to the peer that was already told the new version was accepted.

This breaks the "served vs committed" equality: the `StackerDBChunkInvData` sent back to the requesting peer claims a slot version that the node has not (and may never) actually persisted.

### Impact Explanation
Peers rely on `StackerDBChunkInv` responses to decide what to fetch and to compute rarity/priority for further replication (see stackslib/src/net/stackerdb/mod.rs:85-96, the module doc on the download-schedule logic). A neighbor that receives an inventory claiming a newer slot_version than what is truly stored will believe the chunk is available from this node and skip re-fetching or re-propagating it elsewhere, causing legitimate data (e.g., signer messages in a StackerDB used for block-commit signaling) to silently fail to propagate through this node while its false inventory suppresses other peers from re-requesting it — a form of serving non-canonical/false inventory data as canonical, which can stall StackerDB-based propagation.

### Likelihood Explanation
This requires no privileged access — any connected, unauthenticated-at-the-application-layer p2p neighbor can trigger `handle_unsolicited_StackerDBPushChunk` by simply pushing a validly-signed `StackerDBPushChunk` message (the signature only needs to be valid per the slot's *own signer*, which the attacker does not need to control themselves if they are relaying a genuine chunk with a version that will hit the write-frequency throttle on the receiving node, e.g. shortly after that node already wrote the previous version). The condition is reachable in normal churn/timing scenarios (rapid re-pushes), not just adversarial ones, making it a plausible, low-effort trigger.

### Recommendation
Do not patch and send the `StackerDBChunkInvData` reply optimistically based only on `validate_received_chunk`. Either:
- Perform the actual `try_replace_chunk` write (or an equivalent full check including `write_freq`/slot-existence) synchronously before constructing/sending the inventory reply, or
- Defer sending the chunk-accepted inventory reply until after the relayer has confirmed the chunk was actually stored (e.g., have `process_stacker_db_chunks` trigger the outgoing inv update only upon successful `try_replace_chunk`), so the wire-visible inventory state always matches the true DB state.

### Proof of Concept
1. Configure a StackerDB slot with a nonzero `write_freq` (e.g. `write_freq = 120`).
2. Have peer A write a valid chunk at `slot_version = N` to node B via `StackerDBPushChunk`. Node B stores it successfully.
3. Immediately afterward (well within `write_freq` seconds), peer A (or a relaying third party who obtained a validly-signed chunk at `slot_version = N+1`) pushes another `StackerDBPushChunk` for the same slot at `slot_version = N+1` to node B.
4. `handle_unsolicited_StackerDBPushChunk` runs `validate_received_chunk`, which does not check `write_freq`, passes validation (version `N+1 > N`, within max_writes, correctly signed), patches `StackerDBChunkInvData.slot_versions[slot_id] = N+1`, and immediately replies to peer A with this inventory.
5. Later, the relayer calls `process_pushed_stacker_db_chunks` → `try_replace_chunk`, which internally is expected to enforce `write_freq` via the slot's write-timestamp bookkeeping; if the write is rejected/throttled at this stage (see the `warn!("Failed to store chunk for StackerDB")` branch at stackslib/src/net/relay.rs:2424-2433), the DB retains `slot_version = N`, but node B has already told peer A (and any subsequent syncer) that it holds `slot_version = N+1`.
6. Peers polling node B's inventory will believe chunk `N+1` is available there and may not re-request it from elsewhere, causing that chunk version to be under-replicated or lost from B's perspective while inventory claims otherwise.

Note: I was not able to directly confirm within the indexed portion of `db.rs` that `try_replace_chunk`'s underlying SQL/write-timestamp logic enforces `write_freq` (the snippet retrieved shows chunk-size, signer, staleness, and max-writes checks, but the write-frequency gate implementation itself — likely enforced elsewhere via `slot_validation.write_time` comparison — was not fully visible in the available context). This should be verified in a live session, but the doc comment on `validate_received_chunk` explicitly states write-frequency is intentionally not checked there and deferred to "the caller," and the caller (this unsolicited-push path) does not re-check it before replying, which is the core of the finding regardless of the exact throttle implementation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L641-648)
```rust
    /// Validate chunk data either downloaded (with [`StackerDBSync::validate_downloaded_chunk`]), or
    /// pushed to us (with [`PeerNetwork::handle_unsolicited_StackerDBPushChunk`])
    ///
    /// NOTE: does not check write frequency, since the caller has different ways of doing this.
    /// Returns:
    /// - Ok(true) if the chunk is valid
    /// - Ok(false) if the chunk is invalid
    /// - Err(..) on DB error
```

**File:** stackslib/src/net/stackerdb/mod.rs (L708-718)
```rust
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

**File:** stackslib/src/net/mod.rs (L2249-2254)
```rust
                    StacksMessageType::StackerDBPushChunk(chunk_data) => {
                        self.pushed_stackerdb_chunks.push(PushedStackerDBChunk {
                            peer: neighbor_addr.clone(),
                            chunk: chunk_data,
                        })
                    }
```

**File:** stackslib/src/net/relay.rs (L2409-2437)
```rust
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

**File:** stackslib/src/net/relay.rs (L2471-2493)
```rust
    pub fn process_pushed_stacker_db_chunks(
        &mut self,
        rc_consensus_hash: &ConsensusHash,
        stackerdb_configs: &HashMap<QualifiedContractIdentifier, StackerDBConfig>,
        stackerdb_chunks: Vec<PushedStackerDBChunk>,
        event_observer: Option<&dyn StackerDBEventDispatcher>,
    ) -> Result<(), Error> {
        // synthesize StackerDBSyncResults from each chunk
        let sync_results = stackerdb_chunks
            .into_iter()
            .map(|pushed| {
                debug!("Received pushed StackerDB chunk {:?}", pushed.chunk);
                StackerDBSyncResult::from_pushed_chunk(pushed.chunk, pushed.peer)
            })
            .collect();

        self.process_stacker_db_chunks(
            rc_consensus_hash,
            stackerdb_configs,
            sync_results,
            event_observer,
        )
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L400-438)
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
    }
```
