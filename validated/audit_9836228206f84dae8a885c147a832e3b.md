### Title
StackerDB HTTP chunk-write endpoint never enforces the `write-freq` rate limit, allowing unlimited-rate slot overwrites - (File: stackslib/src/net/stackerdb/db.rs)

### Summary
`StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs:400-438`), which is the only write path used by the public `POST /v2/stackerdb/:principal/:contract/chunks` RPC handler (`stackslib/src/net/api/poststackerdbchunk.rs:197-201`), checks chunk size, slot ownership/signature, version staleness, and `max_writes`, but never checks the `write_freq` value that is defined in `StackerDBConfig` and is documented as "minimum wall-clock time between writes to the same slot." As a result, any principal who owns a slot (i.e., possesses the corresponding private key, which for signer StackerDBs is not a secret admin credential but a normal per-signer key) can write to that slot as fast as the network allows, completely bypassing the wall-clock rate limit that the controlling smart contract configured.

### Finding Description
`StackerDBConfig::write_freq` is loaded straight from the contract's `stackerdb-get-config` call (`stackslib/src/net/stackerdb/config.rs:422-437`) and is documented as the "minimum wall-clock time between writes to the same slot" [1](#0-0) . The `chunks` table stores a `write_time` column specifically to support this check [2](#0-1) , and `get_slot_write_timestamps` exists to read it back [3](#0-2) .

However, the only enforcement of `write_freq` found in the read/relay path is on the *download scheduling* side of P2P sync (`StackerDBSync::make_chunk_request_schedule` skips re-fetching a chunk if it was written too recently) [4](#0-3) , and on throttling the sync state machine's own polling loop [5](#0-4) . Neither of these actually prevents a *write*.

The authoritative write function, `StackerDBTx::try_replace_chunk`, performs exactly these checks — size, slot existence, signer verification, staleness, and `max_writes` — and then calls `insert_chunk`, with no `write_freq`/`write_time` comparison anywhere in the function: [6](#0-5) 

The `Error::TooFrequentSlotWrites` variant exists in the error enum and is formatted for display [7](#0-6) , indicating the rate limit was intended to be enforced as a distinct rejection code, but no call site in `stackslib/src/net/**` actually constructs `Error::TooFrequentSlotWrites`. The peer-to-peer chunk validator `validate_received_chunk` explicitly documents that it skips the write-frequency check, deferring to "the caller" [8](#0-7) , but the caller (`try_replace_chunk`, invoked from `insert_chunk`'s transaction path) never performs it either. The HTTP endpoint `poststackerdbchunk.rs` calls `try_replace_chunk` directly and maps its error variants to ACK codes, with an explicit case list that has no entry for `TooFrequentSlotWrites`: [9](#0-8) 

This breaks the intended equality "time-since-last-write(slot) ≥ configured write_freq before write is accepted." The only real constraints left are the monotonic version bump (`slot_version > slot_validation.version`) and `max_writes`, neither of which limits wall-clock write rate — an attacker who owns a slot key can increment the version and push a new signed chunk on every request, as fast as the HTTP/P2P layer permits.

### Impact Explanation
StackerDB slots back live protocol state — most notably the signer message StackerDBs used for Nakamoto block signing coordination (`libsigner`/`stacks-signer` write `BlockResponse`/`StateMachineUpdate` messages into slots owned by signer keys). A malicious or compromised signer (an ordinary, unprivileged network participant relative to the node under attack — no node secret or admin role required, only their own already-authorized signer key) can flood their own slot with valid, differently-versioned chunks far faster than `write_freq` intends. This causes the receiving node's StackerDB replica to accept and propagate churn at an unbounded rate for that slot, driving unnecessary re-broadcast, storage churn, and processing overhead across the network's StackerDB replication and gossip participants — a bounded-compute DoS vector reachable purely by unprivileged, remote write requests to a documented public write endpoint, and by extension replicated peer-to-peer.

### Likelihood Explanation
High. The precondition is only ownership of a single valid slot-signer keypair for a StackerDB with `write_freq > 0` — this is the normal, unprivileged state of any registered signer/participant, not an admin or node secret. The write path (`POST /v2/stackerdb/:principal/:contract/chunks`) is a documented public RPC endpoint with no additional authentication beyond the per-chunk signature already required by protocol design. No race condition or timing window is even needed — the check is simply absent for every call.

### Recommendation
In `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs`), before calling `insert_chunk`, read the slot's last `write_time` (already tracked in `slot_validation`/the `chunks.write_time` column) and reject the write with `Error::TooFrequentSlotWrites` if `now < write_time + config.write_freq`, mirroring the intent already documented in `StackerDBConfig::write_freq` and the unused `Error::TooFrequentSlotWrites` variant. Add the corresponding ack-code mapping in `poststackerdbchunk.rs::try_handle_request` so HTTP clients get a well-formed rejection rather than an unbounded-rate accept.

### Proof of Concept
1. Configure or use an existing StackerDB contract with `write-freq > 0` (e.g., the signer-message StackerDB).
2. As the legitimate owner of slot `N` (holding its private key), repeatedly issue `POST /v2/stackerdb/<principal>/<contract>/chunks` with monotonically increasing `slot_version` and a valid signature on each request, back-to-back with no delay.
3. Observe via `try_replace_chunk`'s checks (`stackslib/src/net/stackerdb/db.rs:400-438`) that every request succeeds regardless of elapsed wall-clock time since the previous accepted write, because no `write_freq`/`write_time` comparison exists on this path — confirmable directly by code inspection, since the only gating conditions are `chunk.len()`, slot signer match, `slot_version <= slot_validation.version`, and `slot_version > max_writes`.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L238-240)
```rust
    pub signers: Vec<(StacksAddress, u32)>,
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

**File:** stackslib/src/net/stackerdb/db.rs (L52-71)
```rust
    r#"
    CREATE TABLE chunks(
        -- associated stacker DB
        stackerdb_id INTEGER NOT NULL,
        -- slot ID
        slot_id INTEGER NOT NULL,
        -- lamport clock of the chunk.
        version INTEGER NOT NULL,
        -- hash of the data to be stored
        data_hash TEXT NOT NULL,
        -- secp256k1 recoverable signature from the stacker over the above columns
        signature TEXT NOT NULL,

        -- the following is NOT covered by the signature
        -- address of the creator of this chunk
        signer TEXT NOT NULL,
        -- the chunk data itself
        data BLOB NOT NULL,
        -- UNIX timestamp when the chunk was written.
        write_time INTEGER NOT NULL,
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

**File:** stackslib/src/net/stackerdb/db.rs (L618-627)
```rust
    /// Get the list of slot write timestamps for a given DB instance at a given reward cycle
    pub fn get_slot_write_timestamps(
        &self,
        smart_contract: &QualifiedContractIdentifier,
    ) -> Result<Vec<u64>, net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let sql = "SELECT write_time FROM chunks WHERE stackerdb_id = ?1 ORDER BY slot_id";
        let args = params![stackerdb_id];
        query_rows(&self.conn, sql, args).map_err(|e| e.into())
    }
```

**File:** stackslib/src/net/stackerdb/sync.rs (L340-352)
```rust
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

**File:** stackslib/src/net/mod.rs (L408-410)
```rust
            Error::TooFrequentSlotWrites(ref deadline) => {
                write!(f, "Too frequent slot writes (deadline={})", deadline)
            }
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-223)
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
```
