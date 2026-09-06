## Title
`try_replace_chunk` never enforces the `write_freq` rate-limit configured for a StackerDB, allowing unlimited-frequency chunk writes to any slot - (File: `stackslib/src/net/stackerdb/db.rs`)

### Summary
`StackerDBConfig::write_freq` is documented as "the minimum wall-clock time between writes to the same slot" [1](#0-0) , and the protocol even defines a dedicated error, `Error::TooFrequentSlotWrites(deadline)`, specifically to reject writes that arrive before this deadline [2](#0-1) . However, the actual chunk-acceptance function `try_replace_chunk` — the sole gate used by both the remote HTTP write path (`poststackerdbchunk.rs`) and the p2p gossip write path (`relay.rs`) — never checks `write_time`/`write_freq` before writing, even though `write_time` is persisted per slot precisely for this purpose.

### Finding Description
`SlotValidation` records a `write_time` per slot [3](#0-2) , and `insert_chunk` updates it with `get_epoch_time_secs()` on every successful write [4](#0-3) . `StackerDBConfig.write_freq` is loaded straight from the contract's `write-freq` field and is meant to bound how often the same slot can be rewritten [5](#0-4) .

Despite this, `try_replace_chunk` — which is the single authoritative acceptance function for storing a chunk (used to serve `POST /stackerdb/.../chunks` in `poststackerdbchunk.rs`, and to store chunks pushed/synced via p2p gossip in `relay.rs::process_stacker_db_chunks`) — only performs four checks: chunk size, slot-signer existence, signature staleness (`slot_version <= slot_validation.version`), and `max_writes`. It never compares `get_epoch_time_secs()` against `slot_validation.write_time + self.config.write_freq` before calling `insert_chunk`:

```
stackslib/src/net/stackerdb/db.rs:400-438
pub fn try_replace_chunk(...) -> Result<(), net_error> {
    // chunk size check
    // slot existence check
    // signature check  -> BadSlotSigner
    // staleness check  -> StaleChunk
    // max_writes check -> TooManySlotWrites
    self.insert_chunk(smart_contract, slot_desc, chunk)   // <- no write_freq/write_time check
}
``` [6](#0-5) 

This mirrors the reported bug class exactly: a time-based constraint (`write_freq`, analogous to the staking lockup period) is defined, tracked in persisted state (`write_time`, analogous to a lockup timestamp), and has a purpose-built rejection code (`TooFrequentSlotWrites`, analogous to unstake-before-lockup rejection) — but the code path that actually performs the state-changing action (`insert_chunk`/`try_replace_chunk`, analogous to `initiateUnstake`/`instantUnstake`) never checks the elapsed-time equality/inequality before proceeding. `validate_received_chunk` in `mod.rs`, which gates p2p-received chunks earlier in the pipeline, explicitly documents that it also skips this check: "NOTE: does not check write frequency, since the caller has different ways of doing this" [7](#0-6)  — but the ultimate caller (`try_replace_chunk`) is the one place this "other way" would need to live, and it does not.

The impact: any signer entitled to write a StackerDB slot (which, in an unprivileged/remote sense, is any actor whose public key is registered as a slot signer — including miners writing to the miners StackerDB via `send_miners_message`, or signers writing signature/vote messages) can post new chunk versions at whatever rate they choose over HTTP or p2p, up to `max_writes` total versions, completely bypassing the intended `write_freq` throttle. Since chunk writes trigger p2p rebroadcast (`process_stacker_db_chunks` broadcasts every accepted chunk to the network, `stackslib/src/net/relay.rs:2445-2452`), this can be used to flood valid-looking StackerDB gossip traffic across the network at a rate the protocol was explicitly designed to prevent, and to exhaust the version budget (`max_writes`) far faster than intended, denying the legitimate use of the DB for the remainder of the reward cycle.

### Impact Explanation
This does not meet the "Critical" bar of unauthenticated write (the writer must still have a registered signer key for the slot) or of unauthenticated crash, but it does allow a remote, otherwise-authorized StackerDB writer to bypass a designed rate limit and force network-wide propagation of chunks at unthrottled frequency, exhausting the `max_writes` budget prematurely and creating bounded but nontrivial gossip load — this best matches the "High" tier: steering shared network state (chunk replication cadence) away from its designed throttling policy, causing a bounded-but-real DoS effect against the intended write budget for all consumers of that StackerDB slot.

### Likelihood Explanation
Likelihood is high for any actor already possessing a signer key registered for a StackerDB slot (e.g. a signer or miner participating normally in the protocol): no special privilege beyond an ordinary write credential is needed, and the exploit merely requires sending chunks faster than `write_freq` intends, which requires no more than repeatedly calling the existing `POST chunk` API or p2p push path.

### Recommendation
Add a `write_freq` enforcement check in `try_replace_chunk` (or in a wrapper used by both HTTP and p2p write paths) that compares `get_epoch_time_secs()` against `slot_validation.write_time + self.config.write_freq`, returning `Error::TooFrequentSlotWrites` when the deadline has not elapsed, consistent with the error variant that already exists for this purpose.

### Proof of Concept
1. Configure a StackerDB contract with a nonzero `write-freq` (e.g. `write-freq: u120`).
2. As a registered slot signer, submit a validly-signed chunk at `slot_version = 1` via `POST /stackerdb/<contract>/chunks` (accepted, per `test_request_ok` in `stackslib/src/net/api/tests/poststackerdbchunk.rs:92-154`).
3. Immediately submit another validly-signed chunk at `slot_version = 2` for the same slot, well within the configured `write_freq` window.
4. Observe the second write is accepted immediately (no `TooFrequentSlotWrites` rejection), because `try_replace_chunk` at `stackslib/src/net/stackerdb/db.rs:400-438` never checks `write_time`/`write_freq`, whereas the config declares and the `Error::TooFrequentSlotWrites` variant exists specifically to prevent this.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L239-240)
```rust
    /// minimum wall-clock time between writes to the same slot.
    pub write_freq: u64,
```

**File:** stackslib/src/net/stackerdb/mod.rs (L641-644)
```rust
    /// Validate chunk data either downloaded (with [`StackerDBSync::validate_downloaded_chunk`]), or
    /// pushed to us (with [`PeerNetwork::handle_unsolicited_StackerDBPushChunk`])
    ///
    /// NOTE: does not check write frequency, since the caller has different ways of doing this.
```

**File:** stackslib/src/net/mod.rs (L244-245)
```rust
    /// too frequent writes to a slot
    TooFrequentSlotWrites(u64),
```

**File:** stackslib/src/net/stackerdb/tests/db.rs (L307-308)
```rust
        assert_eq!(slot_validation.version, 0);
        assert_eq!(slot_validation.write_time, 0);
```

**File:** stackslib/src/net/stackerdb/db.rs (L381-395)
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
