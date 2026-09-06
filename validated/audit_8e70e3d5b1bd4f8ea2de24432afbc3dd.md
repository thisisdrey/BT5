### Title
`try_replace_chunk()` never enforces the StackerDB `write-freq` cooldown before accepting a chunk write - ([File: stackslib/src/net/stackerdb/db.rs])

### Summary
### Finding Description
`StackerDBConfig::write_freq` is a per-slot cooldown ("minimum wall-clock time between writes to the same slot") that a controlling smart contract stipulates via `stackerdb-get-config`. [1](#0-0) 
The chunk-write acceptance path, `StackerDBTx::try_replace_chunk`, only validates chunk size, slot signer/signature, staleness (`slot_version <= slot_validation.version`), and `max_writes`. It never checks `write_time` against `config.write_freq`, and there is no `TooFrequentSlotWrites` check anywhere in this function or in `insert_chunk`. [2](#0-1) 
The unauthenticated-adjacent gossip-side validator, `PeerNetwork::validate_received_chunk`, explicitly documents that it "does not check write frequency, since the caller has different ways of doing this," deferring the cooldown enforcement to the caller. [3](#0-2) 
The only place `write_freq` is actually consulted is in the *sync scheduler*, `StackerDBSync::make_chunk_request_schedule`, which uses it merely to decide whether the local node should bother *requesting* (downloading) a chunk from peers — it skips scheduling a fetch if the chunk was written too recently, purely as a bandwidth optimization, not as an access-control gate. [4](#0-3) 
Meanwhile, the actual HTTP write endpoint (`RPCPostStackerDBChunkRequestHandler::try_handle_request`) calls `tx.try_replace_chunk(...)` directly and maps its `Err` variants to ack codes — but since `try_replace_chunk` never returns `TooFrequentSlotWrites`/`Error::TooFrequentSlotWrites` (defined in `net/mod.rs` but dead on this path), the smart-contract-declared cooldown is not enforced on writes at all. [5](#0-4) 

This is the same equality break as the Lybra `withdraw()` finding: a protocol-declared restriction (the contract's `write-freq`, analogous to the boost lock duration) is checked in one code path (the sync-download scheduler / earned() boost calc) but never checked at the actual state-mutating entry point (`try_replace_chunk` / `withdraw()`).

### Impact Explanation
Any legitimate slot owner (any signer entitled to write via the controlling smart contract) can bypass the deployer-configured `write-freq` cooldown entirely by posting new chunk versions directly over the StackerDB HTTP POST endpoint as fast as their `max_writes` budget allows, rather than waiting the intended wall-clock interval between writes. This defeats a control that application/protocol operators rely on to rate-limit or throttle replication load and application-level update cadence (e.g. signer message cadence, DKG round pacing) per the smart contract's declared semantics. Because the config comment states write-freq is "minimum wall-clock time between writes to the same slot," and this is meant to be an authoritative per-slot rate limit enforced by every replica, failing to enforce it lets a writer flood a slot with rapid updates faster than intended, which can be abused to increase load on all replicas relaying/storing/gossiping the chunk (since every valid, newer-version, properly-signed write is accepted and then broadcast to neighbors) — a bounded compute/replication-amplification effect reachable by an ordinary authorized-but-unprivileged writer, not an attacker needing any special role beyond being one of the contract's listed signers.

### Likelihood Explanation
High likelihood for any StackerDB whose deployed smart contract sets `write-freq > 0` expecting it to be enforced: no special conditions are needed beyond being a valid slot signer (which many StackerDB use-cases, e.g. signer-set contracts, grant broadly to protocol participants). The check is simply absent from the codebase; it requires no race condition or timing trick — a writer can just always submit a valid, incrementing-version, signed chunk.

### Recommendation
Enforce `config.write_freq` inside `StackerDBTx::try_replace_chunk` before calling `insert_chunk`, similar to the other checks already present (signer, staleness, max_writes): load the current slot's `write_time` (already tracked in the `chunks` table) and reject with `net_error::TooFrequentSlotWrites` if `now < write_time + config.write_freq`. This mirrors how `validate_received_chunk`/`make_chunk_request_schedule` already know about `write_freq`, but currently only use it advisory for scheduling fetches rather than as a write-side gate.

### Proof of Concept
1. Deploy/operate a StackerDB whose controlling contract's `stackerdb-get-config` returns `write-freq: u3600` (writes should be limited to once per hour per slot), matching `StackerDBConfig::write_freq` as loaded in `StackerDBConfig::eval_config`. [6](#0-5) 
2. As a signer owning slot `N`, sign and POST `StackerDBChunkData { slot_id: N, slot_version: 1, ... }` to `/v2/stackerdb/.../chunks` (handled by `RPCPostStackerDBChunkRequestHandler`), which succeeds via `try_replace_chunk`. [7](#0-6) 
3. Immediately (well within the `write_freq` window) sign and POST a new chunk with `slot_version: 2` for the same slot. Because `try_replace_chunk` only checks chunk size, signer, `slot_version <= slot_validation.version` (staleness), and `max_writes` — never `write_time` vs `write_freq` — the write succeeds instantly. [8](#0-7) 
4. Repeat step 3 in a tight loop; each write succeeds as long as `slot_version` increments and stays `<= max_writes`, with no cooldown enforced, contrary to the contract's declared `write-freq`.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L238-242)
```rust
    pub signers: Vec<(StacksAddress, u32)>,
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
    /// maximum number of times a slot may be written to during a reward cycle.
    pub max_writes: u32,
```

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

**File:** stackslib/src/net/stackerdb/sync.rs (L320-352)
```rust
        let local_write_timestamps = self
            .stackerdbs
            .get_slot_write_timestamps(&self.smart_contract_id)?;

        if local_slot_versions.len() != local_write_timestamps.len() {
            let msg = format!("{}: Local slot versions ({}) out of sync with DB slot versions ({}); abandoning sync and trying again", &self.smart_contract_id, local_slot_versions.len(), local_write_timestamps.len());
            warn!("{}", &msg);
            return Err(net_error::Transient(msg));
        }

        let mut need_chunks: HashMap<usize, (StackerDBGetChunkData, Vec<NeighborAddress>)> =
            HashMap::new();
        let now = get_epoch_time_secs();

        // who has data we need?
        for ((i, local_version), write_ts) in local_slot_versions
            .iter()
            .enumerate()
            .zip(local_write_timestamps.iter())
        {
            if self.write_freq > 0 && write_ts + self.write_freq > now {
                debug!(
                    "{:?}: {}: Chunk {} was written too frequently ({} + {} > {}) in {}, so will not fetch chunk",
                    network.get_local_peer(),
                    &self.smart_contract_id,
                    i,
                    write_ts,
                    self.write_freq,
                    now,
                    &self.smart_contract_id,
                );
                continue;
            }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-237)
```rust
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
                        NetError::TooManySlotWrites { .. } => {
                            StackerDBErrorCodes::TooManySlotWrites
                        }
                        _ => {
                            error!("Failed to replace StackerDB chunk with an unexpected error";
                                   "smart_contract_id" => contract_identifier.to_string(),
                                   "error" => format!("{:?}", &e)
                            );
                            return Err(StacksHttpResponse::new_error(
                                &preamble,
                                &HttpServerError::new(format!(
                                    "Failed to store StackerDB chunk for {}: {:?}",
                                    &contract_identifier, &e
                                )),
                            ));
                        }
                    };
```

**File:** stackslib/src/net/stackerdb/config.rs (L422-437)
```rust
        let write_freq = config_tuple
            .get("write-freq")
            .expect("FATAL: missing 'write-freq'")
            .clone()
            .expect_u128()?;
        if write_freq > u64::MAX as u128 {
            let reason = format!(
                "Contract {} stipulates a write frequency beyond u64::MAX",
                contract_id
            );
            warn!("{}", &reason);
            return Err(NetError::InvalidStackerDBContract(
                contract_id.clone(),
                reason,
            ));
        }
```
