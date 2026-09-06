Confirmed: `write_freq` is never referenced anywhere in `stackslib/src/net/stackerdb/db.rs`, and `try_replace_chunk` — the sole write-acceptance gate used by both the HTTP POST endpoint (`poststackerdbchunk.rs`) and the P2P relay path (`process_stacker_db_chunks` in `relay.rs`) — checks only chunk size, slot existence, signer, staleness, and `max_writes`, never `write_freq`.### Title
`try_replace_chunk` never enforces the StackerDB's `write_freq` rate limit, letting an unprivileged writer bypass a config-mandated write-frequency restriction — (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
The vMaia bug class was a getter (`maxWithdraw`/`maxRedeem`) that failed to reflect an active pause/restriction and instead reported the unrestricted value. The analogous fault in stacks-core is that the StackerDB write-acceptance function, `StackerDBTx::try_replace_chunk`, never checks the `write_freq` field of `StackerDBConfig` — the field whose entire purpose ("minimum wall-clock time between writes to the same slot") is to restrict how often a slot may legitimately be rewritten — so the restriction that the control smart contract advertises is never actually enforced on writes.

### Finding Description
`StackerDBConfig` declares `write_freq: u64` documented as "minimum wall-clock time between writes to the same slot" [1](#0-0) , and this same requirement is echoed in the smart-contract boilerplate (`stackerdb-get-config` returning `write-freq`) [2](#0-1) .

The single function that actually performs and gates a chunk write, `StackerDBTx::try_replace_chunk`, checks chunk size, slot existence, signer validity, staleness (version), and `max_writes` — but never reads or compares against `self.config.write_freq` or the stored `write_time` column at all: [3](#0-2) 

This is the sole write path invoked both by the unauthenticated HTTP `POST /v2/stackerdb/:address/:contract/chunks` endpoint [4](#0-3)  and by the P2P relay path that stores gossip-received chunks (`process_stacker_db_chunks`) [5](#0-4) .

By contrast, `write_freq` is honored only on the *read/fetch* side — `StackerDBSync` uses it purely to decide whether to bother *requesting* a chunk from a peer during sync [6](#0-5)  — and explicitly not on the validation side either: `validate_received_chunk` documents "NOTE: does not check write frequency, since the caller has different ways of doing this" [7](#0-6) . But no caller in the actual storage path (`try_replace_chunk`) ever does check it — the "different way" referenced in that comment does not exist for locally/HTTP-submitted or relay-stored chunks. The `net_error::TooFrequentSlotWrites` variant even exists in the error enum [8](#0-7)  and is displayed [9](#0-8) , but it is dead code — never constructed or returned anywhere in `db.rs`.

This is a direct analog of the vMaia bug: the config parameter that is supposed to gate/restrict an action (write_freq ↔ withdrawal pause) is silently not enforced at the point where the action is actually authorized (`try_replace_chunk` ↔ `maxWithdraw`/`maxRedeem`), so any signer holding a valid slot key can write far more frequently than the smart-contract-declared policy allows.

### Impact Explanation
Any legitimate slot owner (who possesses only the private key for their own StackerDB slot — not the node's or another party's key) can flood their own slot with an unbounded rate of valid, signed chunk writes/broadcasts, exceeding the wall-clock write-frequency the control smart contract mandates. Every accepted chunk is re-broadcast to the P2P network via `broadcast_message` in `process_stacker_db_chunks` [10](#0-9) , so this converts what should be a rate-limited resource into an unthrottled propagation amplifier across all replicating nodes, consuming network, disk-write, and signature-verification resources network-wide from a single signer's key at a rate the protocol design explicitly intended to cap. It also breaks the correctness invariant that `write_time`/`write_freq` is supposed to protect (e.g., signer message cadence in `signers.clar`-based StackerDBs), since only `max_writes` (a per-cycle count cap) remains as a backstop while write timing is entirely unconstrained.

### Likelihood Explanation
High/certain given the code as written: this requires no special access beyond a normal, already-authorized slot signer key (which every legitimate participant already possesses), no race condition, and no exotic input — a simple loop of valid signed POSTs (or gossip pushes) with monotonically increasing `slot_version` reliably bypasses the `write_freq` restriction on every call, since `try_replace_chunk` contains no time-based gate whatsoever.

### Recommendation
In `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs`), after loading `slot_validation`, compare `get_epoch_time_secs()` against `slot_validation.write_time + self.config.write_freq` and reject with `net_error::TooFrequentSlotWrites(deadline)` if the elapsed time is insufficient, mirroring the existing (unused) error variant and the read-side logic already present in `sync.rs`.

### Proof of Concept
1. Configure/observe a StackerDB whose control contract sets `write-freq` > 0 (e.g., the `signers.clar` boilerplate can set nonzero `write-freq`, or any custom contract).
2. As the owner of slot `i`, sign and `POST` a chunk with `slot_version = 1` to `/v2/stackerdb/:address/:contract/chunks`; it is accepted per `try_replace_chunk`.
3. Immediately (well within the `write_freq` window) sign and POST another chunk for the same slot with `slot_version = 2`. Because `try_replace_chunk` only checks `slot_version > slot_validation.version` (staleness) and `slot_version <= config.max_writes`, and never checks `write_time`/`write_freq`, this second write is accepted immediately, violating the configured minimum interval — confirmed by the absence of any `write_freq`/`write_time` comparison in the function body [11](#0-10) .
4. Repeat as fast as version increments and signing allow; each accepted write is also rebroadcast network-wide via `process_stacker_db_chunks`, amplifying the effect to all replicating peers.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L232-247)
```rust
/// Settings for the Stacker DB
#[derive(Clone, Debug, PartialEq)]
pub struct StackerDBConfig {
    /// maximum chunk size
    pub chunk_size: u64,
    /// list of who writes and how many slots they have
    pub signers: Vec<(StacksAddress, u32)>,
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
    /// maximum number of times a slot may be written to during a reward cycle.
    pub max_writes: u32,
    /// hint for some initial peers that have replicas of this DB
    pub hint_replicas: Vec<NeighborAddress>,
    /// hint for how many neighbors to connect to
    pub max_neighbors: usize,
}
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L50-59)
```text
;; called by .signers-(0|1)-xxx contracts
;; NOTE: the node may ignore `write-freq`, since not all stackerdbs will be needed at a given time
(define-read-only (stackerdb-get-config)
	(ok
		{ chunk-size: CHUNK_SIZE,
		  write-freq: u0, 
		  max-writes: MAX_WRITES,
		  max-neighbors: u32,
		  hint-replicas: (list ) }
	))
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-439)
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

**File:** stackslib/src/net/mod.rs (L244-245)
```rust
    /// too frequent writes to a slot
    TooFrequentSlotWrites(u64),
```

**File:** stackslib/src/net/mod.rs (L408-410)
```rust
            Error::TooFrequentSlotWrites(ref deadline) => {
                write!(f, "Too frequent slot writes (deadline={})", deadline)
            }
```
