### Title
StackerDB write-frequency cooldown (`write_freq`) is never enforced on the authoritative write path, allowing unbounded-rate chunk writes/broadcast - ([File: stackslib/src/net/stackerdb/db.rs])

### Summary
`StackerDBConfig::write_freq` is documented and used as a per-slot wall-clock cooldown for StackerDB writes, but the authoritative chunk-write function, `StackerDBTx::try_replace_chunk`, never checks it. This mirrors the reported Popcorn `harvest()` bug, where a cooldown-gating state variable (`lastHarvest`) was never updated/consulted at the enforcement site, so the cooldown had no real effect.

### Finding Description
`try_replace_chunk` in [1](#0-0)  validates a new chunk against three conditions: chunk size, signer authenticity, `StaleChunk` (version must exceed the stored version), and `TooManySlotWrites` (version must not exceed `config.max_writes`). It never reads or compares against `config.write_freq` or the slot's last `write_time`, even though `SlotValidation` carries a `write_time` field that is updated on every insert via `insert_chunk` ( [2](#0-1) ), and even though `get_slot_validation`/`get_slot_write_timestamps` expose that timestamp for exactly this purpose. This is the same class of bug as the report: a cooldown-relevant timestamp exists and is recorded, but the code path that is supposed to gate a repeated privileged action against it (`try_replace_chunk`, called on every accepted write) never consults it — the equality/threshold check (`elapsed >= write_freq`) is simply absent, so the "cooldown" silently never applies.

This function is the single choke point for all StackerDB writes:
- Inbound RPC writes: `POST /v2/stackerdb/{contract}/chunks` handler in `stackslib/src/net/api/poststackerdbchunk.rs` calls into the DB tx to store a client-signed chunk.
- Peer-to-peer sync/relay writes: `Relayer::process_stacker_db_chunks` calls `tx.try_replace_chunk` for chunks obtained from StackerDB sync with other nodes ( [3](#0-2) ), and on success **re-broadcasts** the chunk to the whole neighbor set ( [4](#0-3) ).

By contrast, `write_freq` is referenced in `stackslib/src/net/stackerdb/sync.rs`, but only as part of the sync engine's own pacing/backoff heuristics for deciding when to re-fetch from peers — it is not enforced as an authorization gate on the actual state mutation in `db.rs`. The only real limiter on write rate for a slot is `max_writes` (a fixed total write-count cap unrelated to time), and version monotonicity (`StaleChunk`), neither of which prevents rapid-fire writes as long as the version strictly increases and stays under `max_writes`.

### Impact Explanation
Any principal who legitimately owns a StackerDB slot (holds the slot's private key — this is not a privileged/admin key, StackerDB slot ownership is a routine, low-trust role assigned per contract, e.g. signer sets) can submit chunk writes at the fastest rate the node/network allows, up to `max_writes` versions, completely ignoring the operator-configured `write_freq` cooldown that the smart-contract-defined `StackerDBConfig` intends to enforce. Because every accepted write is unconditionally re-broadcast to the full neighbor set (`process_stacker_db_chunks`), this converts into a network-wide amplification/spam vector: instead of the intended one-write-per-`write_freq`-interval throughput, an authorized writer can flood updates and force every replicating node to repeatedly validate, store, and gossip chunks at unbounded rate, consuming DB I/O, bandwidth, and downstream event-observer processing (`observer.new_stackerdb_chunks`) across the network. This falls under "network-wide propagation of forged/abusive data" / bounded-compute-DoS class impact, since the cooldown was specifically meant to bound this per-slot write rate and the bound is not enforced.

### Likelihood Explanation
High likelihood: no special privilege beyond normal, low-trust slot ownership is required (any StackerDB participant, e.g., any registered signer for a `.signers-*` contract), the vulnerable function is on the mandatory hot path for every single accepted write, and exploitation only requires signing and submitting new chunk versions back-to-back — something already permitted by the existing (correctly-checked) version/size/signature logic. The only accidental mitigation is `max_writes`, a static ceiling unrelated to time, which merely delays rather than prevents the abuse (and once reached, the slot is a write dead cache — a related but separate note).

### Recommendation
Enforce `write_freq` inside `try_replace_chunk` using the recorded `slot_validation.write_time`, mirroring the report's fix of actually consulting/consuming the cooldown state at the enforcement site:
```rust
if get_epoch_time_secs() < slot_validation.write_time.saturating_add(self.config.write_freq) {
    return Err(net_error::TooFrequentSlotWrites { ... });
}
```
placed alongside the existing `StaleChunk`/`TooManySlotWrites` checks in [5](#0-4) , before calling `insert_chunk`.

### Proof of Concept
1. Configure a StackerDB contract with `write_freq = N` seconds (intended cooldown) and `max_writes = M` (M > 1).
2. As a legitimate slot owner, sign and POST chunk version 1, then immediately (within < N seconds) sign and POST chunk version 2, ..., up to version M, all in rapid succession.
3. Observe (via `stackslib/src/net/stackerdb/db.rs::try_replace_chunk`, [6](#0-5) ) that every write succeeds immediately because only `slot_version <= slot_validation.version` (`StaleChunk`) and `slot_version > config.max_writes` (`TooManySlotWrites`) are checked — `write_freq`/`write_time` is never compared.
4. Each accepted write is broadcast to all neighbors via `Relayer::process_stacker_db_chunks` ( [4](#0-3) ), demonstrating the cooldown provides no actual rate limiting on write/propagation frequency.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L374-396)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L398-437)
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
