## Title
Missing `write_freq` rate-limit enforcement in StackerDB chunk write path enables unbounded write/relay flooding by a legitimate slot owner - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
`StackerDBConfig::write_freq` is documented as "the minimum wall-clock time between writes to the same slot" [1](#0-0)  and is read from the smart-contract-defined StackerDB config [2](#0-1) . However, the function that actually accepts and commits a chunk write, `StackerDBTx::try_replace_chunk`, never checks it — it only checks chunk size, slot existence, signature, staleness, and `max_writes` [3](#0-2) . This is the same class of bug as the `donate()` example: a time-window gate that is supposed to bound repeated writes is never enforced at the point where the write is actually committed.

### Finding Description
`write_freq` is only consulted in one place: `StackerDBSync::readd_chunk_priorities`/download-scheduling logic decides whether to *fetch* a chunk from a peer based on `write_ts + self.write_freq > now` [4](#0-3) . The function that validates a chunk before storing it, `PeerNetwork::validate_received_chunk`, explicitly documents that it does **not** check write frequency, leaving it to "the caller" [5](#0-4) . But the actual write-commit function, `try_replace_chunk`, which both `validate_downloaded_chunk`'s caller and the HTTP `POST /v2/stackerdb/.../chunks` handler ultimately depend on to persist a chunk, performs no `write_freq`/timestamp check at all — it goes straight from signature/staleness/`max_writes` checks to `insert_chunk` [6](#0-5) . `insert_chunk` records `write_time` for informational/scheduling purposes only [7](#0-6) , but nothing ever compares it against `write_freq` before allowing the next write. Consequently the "grace period"/pacing invariant that `write_freq` is meant to enforce is broken at the only place that matters: the write-acceptance path.

The HTTP handler `RPCPostStackerDBChunkRequestHandler::try_handle_request` calls `tx.try_replace_chunk(...)` directly with no additional rate check [8](#0-7) , and on acceptance immediately queues the chunk for network-wide relay via `node.set_relay_message(StacksMessageType::StackerDBPushChunk(...))` [9](#0-8) .

### Impact Explanation
Any principal who legitimately owns a StackerDB slot (holds that slot's private key, e.g. a registered signer) can bump the slot's version and POST as fast as they want, up to `max_writes` times, with zero enforced pacing — even though the StackerDB's own on-chain config specifies a `write_freq` intended to bound this. Each accepted write is queued for relay to the entire replicating peer set, so this converts a config-sanctioned rate limit into an unbounded write/broadcast primitive: the intended per-reward-cycle pacing is defeated, and the network-wide relay of `StackerDBPushChunk` messages can be triggered at a rate far exceeding what the contract's `write-freq` was designed to permit. For a StackerDB contract that sets a meaningful `write-freq` (unlike the built-in `signers.clar`, which sets `write-freq: u0` and is therefore unaffected [10](#0-9) ), this allows disproportionate chunk churn/relay volume from a single already-authorized writer, degrading replication fairness and imposing unbounded relay/storage churn on all replicas of that DB.

### Likelihood Explanation
High for any StackerDB contract that sets a nonzero `write-freq` expecting it to bound write rate: the only requirement is holding a slot's own private key (already granted by contract configuration), and the missing check is unconditionally reachable via the public `POST /v2/stackerdb/:principal/:contract/chunks` endpoint with a validly-signed, incrementing `slot_version`.

### Recommendation
Enforce `config.write_freq` inside `try_replace_chunk` (or immediately before calling it) by comparing the current time against the previously stored `write_time` for the slot (already tracked via `get_slot_validation`/`write_time`), rejecting the write with a dedicated error/ack code (e.g. `WriteTooFrequent`) analogous to `StaleChunk`/`TooManySlotWrites`, mirroring how `write_freq` is already used to gate outbound fetches in `sync.rs`.

### Proof of Concept
1. Configure/deploy a StackerDB contract with `write-freq: u60` (i.e., writes to the same slot must be at least 60s apart) and register a signer key for slot 0.
2. As that signer, sign and `POST` chunk version 1 to `/v2/stackerdb/<addr>/<contract>/chunks` — accepted per `try_replace_chunk`'s checks (size/slot/signature/staleness/`max_writes`) [11](#0-10) .
3. Immediately (sub-second) sign and `POST` chunk version 2, 3, 4, ... in a tight loop. Each passes because no code path compares elapsed time to `write_freq`; only `slot_version` monotonicity and `max_writes` are checked.
4. Each accepted write triggers `node.set_relay_message(StacksMessageType::StackerDBPushChunk(...))`, so the node relays a new push chunk to its peers far more often than the contract-specified `write-freq` should ever allow, confirming the pacing invariant is unenforced.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L239-240)
```rust
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L197-201)
```rust
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L315-324)
```rust
        if ack_resp.accepted {
            let push_chunk_data = StackerDBPushChunkData {
                contract_id: contract_identifier,
                rc_consensus_hash: node.with_node_state(|network, _, _, _, _| {
                    network.get_chain_view().rc_consensus_hash.clone()
                }),
                chunk_data: stackerdb_chunk,
            };
            node.set_relay_message(StacksMessageType::StackerDBPushChunk(push_chunk_data));
        }
```

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L52-59)
```text
(define-read-only (stackerdb-get-config)
	(ok
		{ chunk-size: CHUNK_SIZE,
		  write-freq: u0, 
		  max-writes: MAX_WRITES,
		  max-neighbors: u32,
		  hint-replicas: (list ) }
	))
```
