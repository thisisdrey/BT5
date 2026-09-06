This confirms `write-freq` is a real, contract-configurable knob (arbitrary Clarity contracts can set it, and non-boot contracts can even fail-open to defaults) that is documented as "minimum wall-clock time between writes to the same slot," but the authoritative write path never enforces it.

### Title
`StackerDBTx::try_replace_chunk()` never enforces the configured `write_freq` cooldown, letting any owner-signed writer spam StackerDB slots and force network-wide chunk-push amplification - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
`StackerDBConfig.write_freq` is defined and documented as "minimum wall-clock time between writes to the same slot" [1](#0-0)  and is derived directly from an arbitrary controlling smart contract's `write-freq` return value [2](#0-1) . However, the single authoritative write function, `StackerDBTx::try_replace_chunk`, checks chunk size, signer, staleness, and `max_writes`, but never reads or compares against `write_freq`/`write_time` at all [3](#0-2) .

### Finding Description
The `write_freq` cooldown is only consulted in one place: `StackerDBSync::find_chunks_to_download`, which uses it to decide whether the *local sync state machine* should bother fetching a chunk from peers (`if self.write_freq > 0 && write_ts + self.write_freq > now { ... continue; }`) [4](#0-3) . This is explicitly a best-effort, non-authoritative check — the code comment on `validate_received_chunk` states "NOTE: does not check write frequency, since the caller has different ways of doing this" [5](#0-4) , and `validate_downloaded_chunk` similarly notes "no need to validate the timestamp, because we already skipped requesting it if it was written too recently" [6](#0-5) .

The unsolicited-push handler explicitly documents that write frequency is *not* checked for pushed chunks either, deferring entirely to bandwidth throttling instead: "The write frequency is not checked for this chunk... because the `ConversationP2P`... will have already bandwidth-throttled the remote peer" [7](#0-6) .

Crucially, the RPC write endpoint `POST /v2/stackerdb/.../chunks` calls `try_replace_chunk` directly with no write_freq check anywhere in its path [8](#0-7) , and the relay path that stores synced/pushed chunks (`process_stacker_db_chunks`) also calls `try_replace_chunk` directly [9](#0-8) . In both cases, any party who legitimately owns a slot (i.e., possesses their own valid signing key for that slot — no admin role or another party's key needed) can write a new, validly-signed, version-incrementing chunk as fast as they like, completely ignoring the contract-configured cooldown. The `write_time` column is recorded on every insert [10](#0-9)  but is never read back and compared in the write-gating logic.

This breaks the equality between "the write-frequency the smart contract configures/administrators believe is enforced" and "the write-frequency actually enforced by the node," because the enforcement exists only as an opportunistic peer-sync scheduling optimization, not a real access control.

### Impact Explanation
Because every chunk that `try_replace_chunk` accepts is immediately re-broadcast to all peers (`process_stacker_db_chunks` broadcasts a `StackerDBPushChunk` message for every newly stored chunk regardless of write_freq) [11](#0-10) , and the HTTP POST endpoint also logs/accepts every valid write for storage and gossip [12](#0-11) , a slot-owner (e.g., one signer out of many in a `.signers-*` StackerDB) can write far faster than the cooldown the contract intends, forcing continuous re-validation, storage churn, and push-gossip amplification network-wide for every replica of that StackerDB. This is a bounded-but-real DoS/rate-limit-bypass vector against any StackerDB whose control contract sets a nonzero `write-freq` expecting it to actually throttle writers.

### Likelihood Explanation
High likelihood for any signer/operator who already holds a valid StackerDB slot key (a routine, unprivileged capability by design — every registered signer has one). No special access, secret, or protocol violation is required; only sending consecutive valid `PUT chunk` requests with incrementing versions faster than `write_freq`.

### Recommendation
Enforce `write_freq` inside `StackerDBTx::try_replace_chunk` itself (the single authoritative write path), by comparing `get_epoch_time_secs()` against the stored `write_time` from `get_slot_validation` and rejecting with a dedicated error (e.g. the already-defined but unused `Error::TooFrequentSlotWrites`) [13](#0-12)  when `write_time + write_freq > now`, rather than relying on the sync scheduler's best-effort skip or on TCP-level bandwidth throttling for pushes.

### Proof of Concept
1. Configure (or use) a StackerDB control contract with `write-freq: u3600` and a slot owned by key `k`.
2. As the owner of that slot, repeatedly call `PUT /v2/stackerdb/<contract>/chunks` with strictly increasing `slot_version` values signed by `k`, sending many requests within the same second.
3. Observe that `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-438`) accepts every one of them (bounded only by `max_writes`), each triggering a `StackerDBPushChunk` broadcast to the network, despite the contract's intended 3600-second cooldown never being checked.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L238-240)
```rust
    pub signers: Vec<(StacksAddress, u32)>,
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
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

**File:** stackslib/src/net/stackerdb/mod.rs (L731-734)
```rust
    /// The write frequency is not checked for this chunk. This is because the `ConversationP2P` on
    /// which this chunk arrived will have already bandwidth-throttled the remote peer, and because
    /// messages can be arbitrarily delayed (and bunched up) by the network anyway.
    ///
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

**File:** stackslib/src/net/stackerdb/db.rs (L381-396)
```rust
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

**File:** stackslib/src/net/stackerdb/sync.rs (L335-352)
```rust
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

**File:** stackslib/src/net/stackerdb/sync.rs (L537-558)
```rust
    /// Validate a downloaded chunk
    pub fn validate_downloaded_chunk(
        &self,
        network: &PeerNetwork,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
    ) -> Result<bool, net_error> {
        // validate -- must be a valid chunk
        if !network.validate_received_chunk(
            &self.smart_contract_id,
            config,
            data,
            &self.expected_versions,
        )? {
            return Ok(false);
        }

        // no need to validate the timestamp, because we already skipped requesting it if it was
        // written too recently.

        Ok(true)
    }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L271-299)
```rust
                let slot_metadata = if let Ok(Some(md)) =
                    tx.get_slot_metadata(&contract_identifier, stackerdb_chunk.slot_id)
                {
                    md
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(
                            "Failed to load slot metadata after storing chunk".to_string(),
                        ),
                    ));
                };

                if let Err(e) = tx.commit() {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpServerError::new(format!("Failed to commit StackerDB tx: {:?}", &e)),
                    ));
                }

                crate::net::stackerdb::log_stored_stackerdb_chunk(
                    &contract_identifier,
                    &stackerdb_chunk,
                    &crate::net::stackerdb::StackerDBChunkOrigin::Http { peer: http_peer },
                );

                // success!
                let ack = StackerDBChunkAckData {
                    accepted: true,
```

**File:** stackslib/src/net/relay.rs (L2406-2412)
```rust
        for (sc, sync_results) in sync_results_map.into_iter() {
            if let Some(config) = stackerdb_configs.get(&sc) {
                let tx = self.stacker_dbs.tx_begin(config.clone())?;
                for sync_result in sync_results.into_iter() {
                    for (origin, chunk) in sync_result.chunks_to_store.into_iter() {
                        let md = chunk.get_slot_metadata();
                        if let Err(e) = tx.try_replace_chunk(&sc, &md, &chunk.data) {
```

**File:** stackslib/src/net/relay.rs (L2445-2452)
```rust
                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
```

**File:** stackslib/src/net/mod.rs (L408-410)
```rust
            Error::TooFrequentSlotWrites(ref deadline) => {
                write!(f, "Too frequent slot writes (deadline={})", deadline)
            }
```
