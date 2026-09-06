### Title
`write_freq` rate limit is never enforced on the StackerDB write path, letting a valid slot signer flood signed chunks that get network-wide broadcast - (File: stackslib/src/net/stackerdb/db.rs, stackslib/src/net/api/poststackerdbchunk.rs, stackslib/src/net/relay.rs)

### Summary
The `StackerDBConfig::write_freq` field is documented as "minimum wall-clock time between writes to the same slot" [1](#0-0)  — i.e. an operator-configured guarantee that bounds how fast any given signer can churn a slot, which in turn bounds gossip amplification of `StackerDBPushChunk` messages. Like the "rage quit" case, this is a protective, operator/protocol-set parameter that legitimate remote parties (slot signers, and the network at large that must relay their writes) rely on being enforced consistently. In practice, the enforcement point (`StackerDBTx::try_replace_chunk` / `insert_chunk`) never checks it — the check exists only as an optional throttle inside the *puller's own* sync-scheduling logic, not at the point where a chunk is actually admitted and stored.

### Finding Description
`try_replace_chunk`, the sole gate that decides whether a submitted chunk is written to the local StackerDB replica, only checks chunk size, valid slot signer/signature, staleness (`slot_version <= slot_validation.version`) and `max_writes`. It never reads `self.config.write_freq` nor compares against `write_time`: [2](#0-1) 

`insert_chunk`, called at the end of `try_replace_chunk`, blindly updates `write_time` to "now" on every accepted write, but this is only used for bookkeeping, never for rejecting fast repeated writes: [3](#0-2) 

This `try_replace_chunk` gate is reached from two remote-facing paths in scope:
1. The unauthenticated-except-for-slot-signature HTTP `POST /v2/stackerdb/.../chunks` endpoint, which decodes an attacker-supplied `StackerDBChunkData` and calls `try_replace_chunk` with no additional rate check: [4](#0-3) 
2. The p2p relay path `process_stacker_db_chunks`, which stores chunks received from a peer replica via `tx.try_replace_chunk(&sc, &md, &chunk.data)` and then unconditionally re-broadcasts every chunk that was successfully stored to `self.p2p.broadcast_message(vec![], msg)`: [5](#0-4) 

The only place `write_freq` is actually consulted is in the *pull*-side scheduler `StackerDBSync::make_chunk_request_schedule`, which skips fetching a chunk if it was written too recently — but this is a courtesy optimization for the requester, not an admission-control check on the writer: [6](#0-5)  and the sync loop's own throttle in `run()` only limits how often *this node* runs a sync round, not how often a remote signer may push data [7](#0-6) .

Likewise, `validate_received_chunk` (used for unsolicited pushes and the FutureView-Nack buffering path) explicitly documents that it "does not check write frequency, since the caller has different ways of doing this" [8](#0-7)  — but no caller in the write-commit path (`try_replace_chunk`/`process_stacker_db_chunks`) actually performs that check either. The "different way" referenced does not exist on the write-acceptance path.

This breaks the equality that operators/consumers of a StackerDB expect: "a signer may write to its slot no more often than `write_freq` seconds" vs. what is actually enforced ("a signer may write to its slot as often as `max_writes` allows, with no time-based throttle"). The check is silently absent at the only place capable of enforcing it (the DB write transaction), exactly analogous to the rage-quit report's core issue: a protective, protocol-level guarantee (rage-quit deadline / write-rate limit) that other parties structurally depend on is not actually enforced where it matters, and can be bypassed by the party that controls the input (party host / slot signer).

### Impact Explanation
Any holder of a valid StackerDB slot-signing key (a legitimate, unprivileged network participant from the P2P/HTTP layer's perspective — no node secret or admin role required) can submit chunk writes at an unbounded rate via either the RPC POST endpoint or via unsolicited `StackerDBPushChunk` p2p messages. Each accepted write is unconditionally rebroadcast to all connected peers by `process_stacker_db_chunks`, so this becomes a network-wide amplification/DoS vector on StackerDB storage, disk I/O, and p2p bandwidth — well beyond what the `write_freq` config was designed to bound. This matches the "bounded compute DoS on a read endpoint" / write-storm class of High-severity issues described in scope, since it defeats an explicit, per-replica rate-limiting guarantee that other nodes and the write-budget (`max_writes` over a reward cycle) design relies on to bound aggregate write volume over time.

### Likelihood Explanation
High. No cryptographic breaks, no consensus manipulation, and no privileged role are required — only a valid signer key for one slot in a configured StackerDB (a normal, expected participant, e.g. a Nakamoto signer or app operator). The write endpoint (`POST /v2/stackerdb/.../chunks`) is a standard unauthenticated-except-by-signature HTTP RPC route, and the p2p relay path processes chunks from any connected/gossiping peer.

### Recommendation
Enforce `config.write_freq` inside `StackerDBTx::try_replace_chunk` (or immediately before calling it in both `poststackerdbchunk.rs` and `relay.rs::process_stacker_db_chunks`), by comparing the stored `SlotValidation.write_time` against `get_epoch_time_secs()` and rejecting with a dedicated error (e.g. `net_error::TooFrequentWrite`) if `write_time + write_freq > now`, mirroring the existing staleness/`max_writes` checks at lines 424-436 of `stackslib/src/net/stackerdb/db.rs`.

### Proof of Concept
1. Configure a StackerDB contract with `write-freq: u3600` (intended: at most one write per hour per slot) and `max-writes` large (e.g. `u4096`).
2. As the owner of slot 0's signing key, submit `POST /v2/stackerdb/<addr>/<contract>/chunks` repeatedly (version 1, 2, 3, …) back-to-back with no delay, each properly signed by the slot's private key.
3. Observe (via `test_stackerdb_insert_query_chunks` style test on `try_replace_chunk`, see `stackslib/src/net/stackerdb/tests/db.rs:330-482`) that every increasing-version, correctly-signed, within-size, within-`max_writes` chunk is accepted immediately with no `write_freq`-based rejection, contradicting the configured `write_freq`.
4. Each accepted write triggers `process_stacker_db_chunks`'s broadcast to all peers when relayed peer-to-peer (`stackslib/src/net/relay.rs:2445-2452`), so the flood propagates network-wide instead of being throttled at the source as the config intends.

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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L96-120)
```rust
#[derive(Debug, Clone, PartialEq)]
pub enum StackerDBErrorCodes {
    /// The slot already holds a chunk whose version is at least the one submitted.
    DataAlreadyExists,
    /// The chunk's slot ID is out of range for this replica's slot allocation.
    NoSuchSlot,
    /// The chunk's signature does not recover to the address that owns the slot.
    BadSigner,
    /// The chunk exceeds the replica's configured chunk size.
    ChunkTooBig,
    /// The chunk's slot version exceeds the replica's configured maximum writes.
    TooManySlotWrites,
}

impl StackerDBErrorCodes {
    pub fn code(&self) -> u32 {
        match self {
            Self::DataAlreadyExists => 0,
            Self::NoSuchSlot => 1,
            Self::BadSigner => 2,
            Self::ChunkTooBig => 3,
            Self::TooManySlotWrites => 4,
        }
    }

```

**File:** stackslib/src/net/relay.rs (L2406-2452)
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

**File:** stackslib/src/net/stackerdb/sync.rs (L1427-1436)
```rust
        // throttle to write_freq
        if self.last_run_ts + config.write_freq.max(1) > get_epoch_time_secs() {
            debug!(
                "{:?}: {}: stacker DB sync is throttled until {}",
                network.get_local_peer(),
                &self.smart_contract_id,
                self.last_run_ts + config.write_freq
            );
            return Ok(None);
        }
```
