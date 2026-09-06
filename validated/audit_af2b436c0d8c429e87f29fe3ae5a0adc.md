## Title
StackerDB write-frequency (`write_freq`) throttle is never enforced on the chunk-write path, allowing unbounded chunk-write/broadcast spam by any authorized slot signer - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
The StackerDB control-plane contract lets an application bound "how often a slot can be written to (in wall-clock time)" via `StackerDBConfig::write_freq`. However, the only function that actually authorizes and commits a chunk write, `StackerDBTx::try_replace_chunk`, never checks `write_freq`. Both the P2P push path and the public HTTP `POST /v2/stackerdb/.../chunks` endpoint funnel into this same function, so any principal holding a valid slot signing key can write and have the node broadcast new chunk versions to the entire peer network at unlimited frequency, completely bypassing the rate limit the smart contract intended to enforce.

### Finding Description
`StackerDBConfig::write_freq` is documented as "minimum wall-clock time between writes to the same slot" [1](#0-0) , and the control-plane docs state the smart contract controls "how often a slot can be written to (in wall-clock time)" [2](#0-1) .

The actual DB write function, `StackerDBTx::try_replace_chunk`, checks chunk size, slot existence, signer validity, staleness, and `max_writes` — but never checks `write_freq`/`write_time` at all: [3](#0-2) 

The P2P-facing validator `PeerNetwork::validate_received_chunk` explicitly documents this gap: "NOTE: does not check write frequency, since the caller has different ways of doing this" [4](#0-3) . But the caller that actually commits chunks, `process_stacker_db_chunks`, calls `try_replace_chunk` directly with no write-frequency gate before broadcasting the chunk to all peers: [5](#0-4) 

The only place `write_freq` is actually consulted is in the *download-scheduling* logic of `StackerDBSync`, which uses it merely to decide whether the local node should bother re-fetching a chunk from a remote peer — it has no bearing on whether a locally-submitted or pushed write is accepted: [6](#0-5) 

The public HTTP write endpoint, `RPCPostStackerDBChunkRequestHandler`, accepts any well-formed, correctly-signed chunk and calls `tx.try_replace_chunk` directly; its only rejection codes are `DataAlreadyExists`, `NoSuchSlot`, `BadSigner`, `ChunkTooBig`, and `TooManySlotWrites` — there is no `TooFrequentSlotWrites` code path wired in at all, even though the `net_error::TooFrequentSlotWrites` variant exists in the error enum: [7](#0-6) [8](#0-7) 

This is analogous to the referenced report's bug class: a protective control (there, "liquidation lockout"; here, "write-rate throttle") that is defined by policy/config but whose enforcement point silently fails to check it, letting an authorized-but-adversarial actor (a valid slot signer) evade the intended limiter and abuse a downstream, more powerful mechanism (network-wide chunk broadcast/propagation).

### Impact Explanation
Any actor who legitimately controls one StackerDB slot's private key (e.g., a signer or miner whose slot allocation is governed by a control contract that sets a nonzero `write_freq` specifically to bound write/broadcast rate) can submit new chunk versions as fast as the network allows, up to `max_writes`. Each accepted write is committed to local storage and unconditionally rebroadcast to all connected DB-replica neighbors via `broadcast_message` in `process_stacker_db_chunks`. This produces unbounded, network-wide message amplification and storage churn from a single authorized key, unconstrained by the config value operators believe is bounding this behavior. This is a network-wide propagation/DoS impact stemming from a genuine authorization-vs-enforcement mismatch in the write path, not merely local traffic volume against the attacker's own node — the broadcast fan-out affects every replicating peer.

### Likelihood Explanation
High. No privileged access, no cryptographic break, and no chain-consensus involvement are required — only possession of a single valid slot-signer private key, which by design is meant to be held by ordinary, non-privileged application participants (e.g., signers/miners). The vulnerable code path (`try_replace_chunk`) is reached directly by the public, unauthenticated-by-node-operator HTTP endpoint `POST /v2/stackerdb/:principal/:contract/chunks`, requiring no special network position.

### Recommendation
Enforce `config.write_freq` inside `StackerDBTx::try_replace_chunk` (or immediately before it is called from both `poststackerdbchunk.rs` and `relay.rs::process_stacker_db_chunks`/the P2P push handler) by comparing the current time against the slot's stored `write_time` and rejecting with `net_error::TooFrequentSlotWrites` if the deadline has not elapsed, mirroring the existing (but currently unused for this purpose) error variant. Add an explicit `StackerDBErrorCodes::TooFrequentSlotWrites` mapping in `poststackerdbchunk.rs` so HTTP clients receive a proper rejection ack instead of the write silently succeeding and being broadcast.

### Proof of Concept
1. Configure/observe a StackerDB contract with `write_freq > 0` (e.g., a signer-coordination StackerDB intended to rate-limit writes) and obtain the private key for one owned slot.
2. Repeatedly call `POST /v2/stackerdb/<principal>/<contract>/chunks` with monotonically increasing `slot_version` and a fresh valid `SlotMetadata::sign` signature, at a rate far exceeding `write_freq` (e.g., every few milliseconds instead of the configured interval), keeping `slot_version <= max_writes`.
3. Observe in `stackslib/src/net/api/poststackerdbchunk.rs` that every request succeeds (`accepted: true`) because `try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs`) performs no wall-clock check against `write_time`/`write_freq`.
4. Observe that each accepted write triggers a `StackerDBPushChunk` broadcast to all peers via `PeerNetwork::process_stacker_db_chunks` in `stackslib/src/net/relay.rs`, demonstrating unbounded network-wide propagation despite the configured `write_freq` throttle.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L59-62)
```rust
/// The smart contract to which a StackerDB is bound controls how many slots the DB has, who can
/// write to which slots (identified by public key hash), how big a slot is, and how often a
/// slot can be written to (in wall-clock time).  This smart contract is queried once per reward cycle
/// in order to configure the database.
```

**File:** stackslib/src/net/stackerdb/mod.rs (L239-240)
```rust
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L96-153)
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

    #[cfg_attr(test, mutants::skip)]
    pub fn reason(&self) -> &'static str {
        match self {
            Self::DataAlreadyExists => "Data for this slot and version already exist",
            Self::NoSuchSlot => "No such StackerDB slot",
            Self::BadSigner => "Signature does not match slot signer",
            Self::ChunkTooBig => "Chunk exceeds the replica's configured chunk size",
            Self::TooManySlotWrites => {
                "Slot version exceeds the replica's configured maximum writes"
            }
        }
    }

    pub fn into_json(self) -> serde_json::Value {
        json!({
            "code": self.code(),
            "message": format!("{:?}", &self),
            "reason": self.reason()
        })
    }

    #[cfg_attr(test, mutants::skip)]
    pub fn from_code(code: u32) -> Option<Self> {
        match code {
            0 => Some(Self::DataAlreadyExists),
            1 => Some(Self::NoSuchSlot),
            2 => Some(Self::BadSigner),
            3 => Some(Self::ChunkTooBig),
            4 => Some(Self::TooManySlotWrites),
            _ => None,
        }
    }
}
```

**File:** stackslib/src/net/mod.rs (L408-410)
```rust
            Error::TooFrequentSlotWrites(ref deadline) => {
                write!(f, "Too frequent slot writes (deadline={})", deadline)
            }
```
