### Title
StackerDB write-frequency throttle (`write_freq`) is enforced only in the P2P sync path and is silently bypassed via the HTTP `POST /v2/stackerdb/.../chunks` write path - ([File: stackslib/src/net/stackerdb/db.rs])

### Summary
`StackerDBTx::try_replace_chunk()`, the single choke-point used to persist a StackerDB chunk write, only enforces signature validity, monotonic version, and `max_writes`. It never enforces `StackerDBConfig::write_freq` (the minimum wall-clock interval between successive writes to a slot). The `write_freq` throttle appears to be applied only inside the P2P StackerDB sync state machine, not inside the storage layer itself, so any remote caller that reaches `try_replace_chunk()` through a different path is not subject to it.

### Finding Description
`try_replace_chunk()` in `stackslib/src/net/stackerdb/db.rs` performs exactly these checks before calling `insert_chunk()`: [1](#0-0) 
- chunk size vs `config.chunk_size`
- slot exists (`get_slot_validation`)
- signature verifies against the slot's owner (`slot_desc.verify`)
- `slot_version > slot_validation.version` (not stale)
- `slot_version <= config.max_writes`

There is no check against `config.write_freq` or the slot's last `write_time` anywhere in this function, and the doc comment on the analogous P2P validator explicitly states this is deliberate: `validate_received_chunk()` in `stackslib/src/net/stackerdb/mod.rs` is annotated "NOTE: does not check write frequency, since the caller has different ways of doing this." [2](#0-1) 

The RPC handler for the public HTTP write endpoint, `RPCPostStackerDBChunkRequestHandler::try_handle_request` in `stackslib/src/net/api/poststackerdbchunk.rs`, calls `tx.try_replace_chunk(&contract_identifier, &stackerdb_chunk.get_slot_metadata(), &stackerdb_chunk.data)` directly, with no `write_freq`/`write_time` gate performed beforehand: [3](#0-2) 

The `write_freq` value is referenced only in `stackslib/src/net/stackerdb/sync.rs`, which is the internal state machine that schedules and paces *outbound* download/replication activity between peers — not a guard invoked from the inbound HTTP write endpoint. This means the exact same equality/window check that the P2P sync layer relies on ("has enough time elapsed since slot's `write_time` to permit another write") is not present on the code path that actually persists attacker-supplied data from an arbitrary remote HTTP client holding a valid slot-signing key.

This mirrors the Morph L2 bug class: a time-window/throttle invariant is computed and enforced in one code path (the P2P inventory/challenge-adjacent logic) but the actual state-mutating operation (chunk commit / batch commit) reachable through another path does not carry the compensating check, so the intended pacing/window guarantee can be silently skipped.

### Impact Explanation
Any legitimate slot owner (someone holding the private key corresponding to a `signer` configured for a StackerDB slot) can call the public HTTP endpoint `POST /v2/stackerdb/{address}/{contract}/chunks` and write to their slot as fast as the network round-trip permits, regardless of the StackerDB's configured `write_freq`. `write_freq` is a deliberate pacing control used by consumers of StackerDB (e.g., signer message contracts) to bound the rate of state churn and gossip amplification per slot. Bypassing it via the direct HTTP write path allows a slot owner to flood their own slot with rapid successive versions, which will be broadcast to peers via `process_stacker_db_chunks` in `stackslib/src/net/relay.rs` each time storage succeeds: [4](#0-3) 
This amplifies unauthorized-rate network-wide propagation of StackerDB writes beyond the rate the protocol design assumes, potentially straining downstream consumers/observers that assume `write_freq`-bounded update rates, and increasing gossip/bandwidth load network-wide from a single authorized-but-unthrottled writer. It does not, however, allow forging data belonging to another principal (signatures are still checked), and is bounded by needing a valid slot-owner key, so it does not rise to "unauthorized write" or "forged-data propagation" by an unprivileged party in the strict sense used by the rules.

### Likelihood Explanation
Any StackerDB slot owner can trigger this without any race condition, timing coincidence, or cooperation from a third party — a single owned private key and repeated HTTP POSTs are sufficient, since the RPC handler unconditionally forwards to `try_replace_chunk()` which has no rate check. This is a deterministic, always-reachable gap rather than a narrow timing race, which is both what makes it easy to trigger and also what limits its severity (it is a self-affecting/own-slot throttle bypass rather than an ability to affect other principals' data).

### Recommendation
Enforce `config.write_freq` inside `StackerDBTx::try_replace_chunk()` (or immediately before it, in `poststackerdbchunk.rs`) by comparing `get_epoch_time_secs()` against the slot's stored `write_time` from `get_slot_validation()`, mirroring the check intended for the P2P path, so the throttle is enforced at the single storage choke-point regardless of which network path reaches it.

### Proof of Concept
1. Configure a StackerDB contract with `write-freq: u3600` (min 1 hour between writes to a slot) and a signer `privk1` owning slot `1`.
2. As `privk1`, sign and POST a chunk at slot version 1 to `/v2/stackerdb/{addr}/{contract}/chunks` — accepted, per `try_handle_request` success path in `stackslib/src/net/api/poststackerdbchunk.rs`.
3. Immediately (no wait), sign and POST version 2 to the same endpoint. `try_replace_chunk()` only checks `slot_version <= max_writes` and `slot_version > slot_validation.version` — both pass — so the write succeeds instantly, despite `write_freq` requiring an hour between writes.
4. Repeat rapidly to observe unthrottled successive chunk commits and corresponding `StackerDBPushChunk` broadcasts via `process_stacker_db_chunks`, confirming the `write_freq` window is not honored on this path.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L405-437)
```rust
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

**File:** stackslib/src/net/stackerdb/mod.rs (L641-656)
```rust
    /// Validate chunk data either downloaded (with [`StackerDBSync::validate_downloaded_chunk`]), or
    /// pushed to us (with [`PeerNetwork::handle_unsolicited_StackerDBPushChunk`])
    ///
    /// NOTE: does not check write frequency, since the caller has different ways of doing this.
    /// Returns:
    /// - Ok(true) if the chunk is valid
    /// - Ok(false) if the chunk is invalid
    /// - Err(..) on DB error
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L169-220)
```rust
        let contract_identifier = self
            .contract_identifier
            .take()
            .ok_or(NetError::SendError("`contract_identifier` not set".into()))?;
        let stackerdb_chunk = self
            .chunk
            .take()
            .ok_or(NetError::SendError("`chunk` not set".into()))?;
        let http_peer = node.http_peer_addr();

        let ack_resp =
            node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                let tx = if let Ok(tx) = network.stackerdbs_tx_begin(&contract_identifier) {
                    tx
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                };
                if let Err(_e) = tx.get_stackerdb_id(&contract_identifier) {
                    // shouldn't be necessary (this is checked against the peer network's configured DBs),
                    // but you never know.
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                }
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
                    test_debug!(
                        "Failed to replace chunk {}.{} in {}: {:?}",
                        stackerdb_chunk.slot_id,
                        stackerdb_chunk.slot_version,
                        &contract_identifier,
                        &e
                    );
                    // Classify the rejection directly from the error. `StaleChunk` is the
                    // only retryable case (the normal version-bump handshake); everything
                    // else is terminal for an identical chunk. Anything unexpected (DB or
                    // internal error) is a server error, not a client-classifiable ack, so
                    // it becomes an HTTP 500 rather than a misleading `accepted: false`.
                    let err_code = match &e {
                        NetError::StaleChunk { .. } => StackerDBErrorCodes::DataAlreadyExists,
                        NetError::NoSuchSlot(..) => StackerDBErrorCodes::NoSuchSlot,
                        NetError::BadSlotSigner(..) | NetError::VerifyingError(..) => {
                            StackerDBErrorCodes::BadSigner
                        }
                        NetError::StackerDBChunkTooBig(..) => StackerDBErrorCodes::ChunkTooBig,
```

**File:** stackslib/src/net/relay.rs (L2406-2453)
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

                        if let Some(event_list) = all_events.get_mut(&sync_result.contract_id) {
                            event_list.push(chunk.clone());
                        } else {
                            all_events.insert(sync_result.contract_id.clone(), vec![chunk.clone()]);
                        }

                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
                    }
```
