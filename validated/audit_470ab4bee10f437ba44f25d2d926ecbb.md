### Title
StackerDB `write_freq` rate limit is never enforced on writes, only used client-side for download scheduling - ([File: stackslib/src/net/stackerdb/db.rs])

### Summary
The `StackerDBConfig.write_freq` field is meant to enforce a minimum interval between successive writes to a given slot (analogous to the fxUSD `redeemCoolDownPeriod`), but the authoritative write path — `StackerDBTx::try_replace_chunk` — never checks it. The only server-side gates applied when accepting a chunk are chunk size, slot existence, signature validity, version monotonicity (`StaleChunk`), and `max_writes`.

### Finding Description
`try_replace_chunk` in [1](#0-0)  performs exactly these checks: chunk size, slot validation existence, signer verification, staleness, and `max_writes` — there is no comparison against `self.config.write_freq` or the slot's last `write_time` anywhere in this function. The insert itself does record `write_time` via `get_epoch_time_secs()` at [2](#0-1) , so the timestamp is tracked, but nothing reads it back to reject a too-frequent write.

`write_freq` is loaded from the controlling smart contract in [3](#0-2)  and stored on `StackerDBConfig`, but the only place it is consumed downstream is `StackerDBSync`, which uses it purely to decide whether *this node* should bother re-downloading a chunk from a *peer* (`stackslib/src/net/stackerdb/sync.rs`, `write_freq` field at lines 54 and referenced elsewhere for scheduling). That is a client-side optimization for the syncing side, not a server-side write gate.

Both authoritative write paths funnel into `try_replace_chunk` without any additional freshness/interval check:
- The HTTP POST endpoint `RPCPostStackerDBChunkRequestHandler::try_handle_request` calls `tx.try_replace_chunk(...)` directly [4](#0-3) .
- The p2p-gossip ingestion path `PeerNetwork::process_stacker_db_chunks` also calls `tx.try_replace_chunk(...)` with no additional check [5](#0-4) .
- `validate_received_chunk`, used to sanity-check both downloaded and pushed chunks, likewise never checks write frequency — its own doc comment states: "NOTE: does not check write frequency, since the caller has different ways of doing this" [6](#0-5) . But tracing all callers shows no caller actually enforces it before calling `try_replace_chunk`.

This is the direct analog of the reported bug class: a protocol-level cooldown/rate-limit (`write_freq`, meant to throttle a signer's write cadence just as `redeemCoolDownPeriod` throttles redemptions) exists in configuration and is documented/intended, but the code path that actually commits state changes never checks it. A signer who legitimately owns a slot (has the private key) can bump `slot_version` and POST/broadcast a new chunk as fast as `max_writes` allows, with zero minimum interval, regardless of what `write-freq` the controlling smart contract stipulates.

### Impact Explanation
Any authorized slot signer (this does not require compromising another party's key — it is the signer's own key) can flood their StackerDB slot with rapid updates up to `max_writes`, each of which is unconditionally accepted and then rebroadcast to peers via `self.p2p.broadcast_message(...)` in `process_stacker_db_chunks` [7](#0-6) . Because `write_freq` was intended by the contract author as the rate-limiting mechanism protecting replicas from being overwhelmed by a single slot owner's write cadence, its total non-enforcement allows write/broadcast amplification that the protocol design explicitly assumed would be bounded. This falls into the "bounded compute DoS" / unauthorized write pattern (write frequency limits are a stated part of the write acceptance contract but are silently not enforced), causing unnecessary network-wide chunk propagation and repeated disk writes on every replicating node far beyond what the StackerDB contract's `write-freq` was designed to allow.

### Likelihood Explanation
High likelihood of triggering: any legitimate StackerDB slot owner (e.g., a signer set member) can exploit this with no special privilege beyond their own already-authorized signing key, simply by issuing writes faster than `write-freq` intends, using the standard `POST /v2/stackerdb/...` endpoint. No cryptographic bypass or trust violation is needed — this is a case where a protective config value is silently unenforced.

### Recommendation
Enforce `write_freq` in `StackerDBTx::try_replace_chunk` by loading the previous slot's `write_time` (via `get_slot_validation`, which is already fetched) and rejecting the write if `get_epoch_time_secs() - slot_validation.write_time < self.config.write_freq`, mirroring the version/`max_writes` checks that already exist in the same function.

### Proof of Concept
1. Configure a StackerDB smart contract with a non-zero `write-freq` (e.g., 3600 seconds) and `max-writes` set to a large number.
2. As the legitimate slot signer, call `POST /v2/stackerdb/<contract>/chunks` with `slot_version = 1`, signed correctly.
3. Immediately call the same endpoint again with `slot_version = 2`, signed correctly, well before `write-freq` seconds have elapsed.
4. Observe both writes succeed via `try_replace_chunk` [8](#0-7)  (only version-monotonicity and `max_writes` are checked — no timing check), and both chunks are broadcast to the network, demonstrating the configured `write-freq` cooldown has no effect on the write path.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L384-395)
```rust
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
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

**File:** stackslib/src/net/stackerdb/mod.rs (L641-649)
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
```
