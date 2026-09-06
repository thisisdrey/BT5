### Title
`write_freq` rate-limit is never enforced on the StackerDB write path, letting any authenticated slot owner flood writes - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
`StackerDBConfig::write_freq` is documented as the "minimum wall-clock time between writes to the same slot" [1](#0-0) , and each chunk row records a `write_time` UNIX timestamp precisely to support this check [2](#0-1) . However, the actual state-mutating write function, `StackerDBTx::try_replace_chunk`, only validates chunk size, slot existence, signer authenticity, staleness (version), and `max_writes` — it never reads or checks `write_time`/`write_freq` before calling `insert_chunk` [3](#0-2) .

### Finding Description
The rate-limit intent (`write_freq`) is only consulted in the client-side sync/download-scheduling logic in `stackslib/src/net/stackerdb/sync.rs`, which decides whether to *bother fetching* a chunk from a remote peer. It is not enforced on the authoritative write path that actually persists a chunk to a replica's local DB. That write path — `try_replace_chunk` (invoked both by the HTTP `POST /v2/stackerdb/.../chunks` handler in `poststackerdbchunk.rs` and by unsolicited/gossip chunk-push handling in `relay.rs::process_stacker_db_chunks`) — validates:
- chunk size vs `config.chunk_size`
- slot existence
- signature vs the slot's assigned signer
- `slot_version` monotonicity (staleness)
- `slot_version` vs `config.max_writes`

but has no analogous check against `write_freq`/`write_time` before calling `insert_chunk`, which unconditionally stamps the new `write_time = get_epoch_time_secs()` [4](#0-3) .

This is directly analogous to the referenced M-05 "Unmitigated" finding: a protective time-window/rate-limit control exists conceptually and is partially wired up (the timestamp is tracked, and one code path — the client fetch scheduler — respects it), but the actual write/state-mutation path never checks it, so the control fails open. Any account holding a valid slot signing key (an "authenticated" but otherwise unprivileged party, since StackerDB signer sets are drawn from e.g. the signer set contract) can submit new chunk versions as fast as the network/API allows, bounded only by `max_writes` and version monotonicity — not by the intended per-slot cadence.

### Impact Explanation
This does not itself break block-validation consensus, but it defeats a rate-limiting invariant relied upon for StackerDB capacity/DoS planning: `write_freq` exists specifically to bound how often a given (already-authenticated) principal can push new data through a replica, which in turn bounds gossip/broadcast volume that `process_stacker_db_chunks` fans out to all StackerDB-replicating neighbors (`self.p2p.broadcast_message`) [5](#0-4) . Because the accept path fails to enforce the interval, an authenticated slot holder can write new versions back-to-back (limited only by `max_writes`), causing amplified network-wide chunk gossip beyond what the StackerDB's configured `write_freq` was meant to permit — a bounded-resource assumption baked into StackerDB capacity/DoS reasoning is silently violated. This is a lesser, defense-in-depth-style gap rather than a full unauthenticated write or forged-data propagation, since a valid slot signature is still required.

### Likelihood Explanation
High, mechanically: any slot owner (a normal, expected StackerDB writer, e.g. a signer) can trivially trigger this by simply issuing consecutive `POST` chunk requests with strictly increasing `slot_version` and no artificial delay; there is no timing-window subtlety required (unlike the cited report's near-epoch-boundary race) — the check is simply absent from `try_replace_chunk` altogether.

### Recommendation
In `StackerDBTx::try_replace_chunk` (`stackslib/src/net/stackerdb/db.rs`), before calling `insert_chunk`, look up the current slot's `write_time` via `get_slot_validation`/`SlotValidation` and reject the write (e.g. with a new `net_error` such as `TooFrequentWrites`) if `get_epoch_time_secs() - existing.write_time < self.config.write_freq`. Mirror this in the HTTP handler's error-code mapping (`poststackerdbchunk.rs`) and in `process_stacker_db_chunks`/`validate_received_chunk` so gossip-received chunks are held to the same cadence limit as directly-posted ones.

### Proof of Concept
1. Configure a StackerDB contract with `write-freq` set to a large value (e.g. 3600 seconds) and `max-writes` set high.
2. As a valid slot signer, POST chunk version 1 to `/v2/stackerdb/<contract>/chunks` — accepted per `try_replace_chunk` [6](#0-5) .
3. Immediately POST chunk version 2, 3, 4, … signed by the same key, with no delay.
4. Observe that all are accepted (only version-monotonicity and `max_writes` are checked), and each triggers a broadcast via `process_stacker_db_chunks`/gossip to all replicating neighbors — despite `write_freq` supposedly requiring a minimum interval between writes to the same slot.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L239-242)
```rust
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
    /// maximum number of times a slot may be written to during a reward cycle.
    pub max_writes: u32,
```

**File:** stackslib/src/net/stackerdb/db.rs (L66-71)
```rust
        -- address of the creator of this chunk
        signer TEXT NOT NULL,
        -- the chunk data itself
        data BLOB NOT NULL,
        -- UNIX timestamp when the chunk was written.
        write_time INTEGER NOT NULL,
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
