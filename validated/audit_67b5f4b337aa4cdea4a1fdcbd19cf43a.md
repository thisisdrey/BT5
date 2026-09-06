### Title
`write-freq` StackerDB rate limit is defined by the control contract but never enforced on the chunk-write path - ([File: stackslib/src/net/stackerdb/db.rs])

### Summary
The `StackerDBConfig.write_freq` field is loaded from the controlling smart contract to bound "the minimum wall-clock time between writes to the same slot" [1](#0-0) , and a dedicated error variant `net_error::TooFrequentSlotWrites` exists to signal a violation of that limit [2](#0-1) . However, the only place `write_freq` is actually consulted is on the *download-decision* path in `StackerDBSync`, where it merely decides whether to bother fetching a chunk from peers [3](#0-2) . The actual state-mutating write path, `StackerDBTx::try_replace_chunk` in `db.rs`, only checks chunk size, slot existence, signer authenticity, version staleness, and `max_writes` — it never checks `write_freq` or the stored `write_time` [4](#0-3) . `TooFrequentSlotWrites` is never constructed or returned anywhere in the codebase (confirmed by a repo-wide search that only found the enum declaration/Display arm in `net/mod.rs`).

### Finding Description
This is the same bug class as the external report: a rate-limit/delay check that is supposed to gate a state-mutating operation is guarded by state/config that is not actually enforced at the point of the write, so the intended delay is trivially bypassable. In the external analog, `firstApprovalBlock[to] > 0` had to be true for the delay check to even run; here, `write_freq` is computed and stored in `StackerDBConfig`, but the code path that performs the actual chunk write (`try_replace_chunk`, invoked from the unauthenticated `POST /v2/stackerdb/{contract}/chunks` HTTP handler) simply never reads `self.config.write_freq` or the slot's stored `write_time`, so the equality "chunk accepted only if `write_time + write_freq <= now`" never gets evaluated on write.

Any signer holding a private key for a slot (which is by design an ordinary, unprivileged StackerDB participant, not an admin) can therefore push new chunk versions to their slot as fast as network/bandwidth permits, limited only by `max_writes` (an unrelated cap on version count over a whole reward cycle) — completely ignoring the smart contract's `write-freq` configuration.

Concretely, the write path is:
`poststackerdbchunk.rs::try_handle_request` → `tx.try_replace_chunk(...)` [5](#0-4) 
→ `StackerDBTx::try_replace_chunk` checks (in order): chunk size, slot exists, signature ownership, version staleness, `max_writes` — and calls `insert_chunk` on success, with no `write_freq`/`write_time` gate anywhere in that sequence [6](#0-5) .

The same unmet expectation extends to the gossip/relay path: `PeerNetwork::validate_received_chunk`, used for both inbound pushed chunks and downloaded chunks, explicitly documents that it "does not check write frequency, since the caller has different ways of doing this" [7](#0-6)  — but the only "other way" that exists (`sync.rs`'s fetch-decision logic) is a client-side heuristic for whether to *request* a chunk, not an authoritative server-side gate on whether to *accept and store* one.

### Impact Explanation
This breaks the intended equality between the contract-configured write cadence and what the replica actually permits, allowing any authorized-but-unprivileged slot owner to flood the network with chunk versions far above the rate the StackerDB's controlling smart contract intends, exhausting bandwidth/storage churn and propagating excessive chunk updates across the StackerDB replication network (bounded compute/bandwidth DoS on a network-wide, read/replication-facing surface) — until the unrelated `max_writes` version cap is hit. It does not grant unauthorized writes (a slot's signature is still required), so it does not reach a Critical unauthorized-write/forgery bar, but it defeats a specific configured protection meant to bound write cadence, which is a High-severity "compute/bandwidth DoS via missing protocol control" on a state-mutating, network-replicated endpoint.

### Likelihood Explanation
High likelihood for any node operator or delegated signer that already legitimately owns slots in a StackerDB (e.g. signer-set contracts): no special privilege beyond an existing, valid slot-owning key is required, and the omission is unconditional (not a race condition) — it can be exercised by simply issuing rapid, validly-signed `POST /v2/stackerdb/{contract}/chunks` requests with monotonically increasing `slot_version`.

### Recommendation
Enforce `write_freq` inside `StackerDBTx::try_replace_chunk` (the authoritative write path), by comparing `get_epoch_time_secs()` against the slot's previously stored `write_time` from `SlotValidation`, returning `net_error::TooFrequentSlotWrites` when the configured minimum interval has not elapsed — mirroring the same style check already prototyped in `sync.rs`'s fetch-decision logic. The HTTP handler in `poststackerdbchunk.rs` should also map this new error case to a dedicated `StackerDBErrorCodes` ack (analogous to how `StaleChunk`, `NoSuchSlot`, `BadSlotSigner`, and `TooManySlotWrites` are already mapped).

### Proof of Concept
Given a StackerDB contract configuring `write-freq: u120` (chunks may only be rewritten once every 120 seconds) and a valid signer key for slot N:
1. Sign and `POST` chunk data for slot N at `slot_version = 1` to `/v2/stackerdb/{contract}/chunks` — succeeds via `try_replace_chunk` (passes all its checks) [8](#0-7) .
2. Immediately (within milliseconds, well under the 120-second `write_freq`) sign and `POST` a new chunk for the same slot at `slot_version = 2`.
3. This second write succeeds as well, because `try_replace_chunk` never inspects `write_freq` or the slot's `write_time`; only `slot_version > slot_validation.version` and `slot_version <= config.max_writes` are checked.
4. Repeat up to `max_writes` times in rapid succession, defeating the contract's configured write cadence entirely.

*Note: this analog is drawn from static code inspection of `db.rs`, `mod.rs`, `sync.rs`, and `poststackerdbchunk.rs`; I was not able to execute the HTTP flow in this environment to empirically confirm the acceptance response, so this should be validated end-to-end (e.g. via the existing `stackslib/src/net/stackerdb/tests/db.rs` / `tests/sync.rs` test harnesses) before being treated as fully confirmed.*

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

**File:** stackslib/src/net/mod.rs (L244-245)
```rust
    /// too frequent writes to a slot
    TooFrequentSlotWrites(u64),
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```
