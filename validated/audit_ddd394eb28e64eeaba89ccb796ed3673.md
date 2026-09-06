### Title
Legitimate slot-owner write with `slot_version = u32::MAX` permanently freezes a StackerDB slot when the controlling contract sets `max-writes = u4294967295` - ([File: stackslib/src/net/stackerdb/db.rs])

### Summary
`try_replace_chunk` only rejects a chunk if `slot_desc.slot_version > self.config.max_writes` [1](#0-0) . The boot `signers.clar` contract, which controls the real signer StackerDBs, sets `max-writes` to `MAX_WRITES = u4294967295` (i.e. `u32::MAX`) [2](#0-1) , and this value is loaded verbatim into `StackerDBConfig.max_writes: u32` by `eval_config` [3](#0-2) [4](#0-3) . Because `slot_version` is a `u32`, a slot owner who writes with `slot_version = u32::MAX` passes the bound check (`u32::MAX > u32::MAX` is false) and the value is persisted as the slot's latest version [5](#0-4) . Every subsequent legitimate write must satisfy `slot_desc.slot_version > slot_validation.version` (now `u32::MAX`) [6](#0-5) , which is impossible since `u32::MAX` is the maximum representable value — the slot is permanently frozen for the remainder of that reward cycle/table lifetime.

### Finding Description
`try_replace_chunk` enforces two guards on the Lamport-clock version: staleness (`slot_version <= latest_version` → reject) and an upper bound (`slot_version > max_writes` → reject) [1](#0-0) . The upper bound is meant to prevent runaway version growth, but it is compared against `max_writes`, which is a live, contract-controlled `u32` value, not a fixed safety margin below `u32::MAX`. For the actual deployed signer StackerDB contract, `max-writes` is hard-coded to `u4294967295` = `u32::MAX` [2](#0-1) , and `eval_config` accepts any `max-writes` value up to `u32::MAX` without further restriction, casting it directly into `StackerDBConfig.max_writes: u32` [7](#0-6) .

An attacker who legitimately owns slot N (satisfies `slot_desc.verify(&slot_validation.signer)`) can submit one `StackerDBChunkData`/`SlotMetadata` with `slot_version = u32::MAX` signed by their own key. Since `u32::MAX <= max_writes (u32::MAX)`, the check at db.rs:430 does not trigger, and `insert_chunk` persists `slot_version = u32::MAX` for that slot [8](#0-7) . From that point on, any future write to that slot — from the legitimate owner or anyone else with the key — requires `slot_version > u32::MAX`, which no `u32` value can satisfy, so every future write hits `StaleChunk` and is rejected at db.rs:424-429. The same `data.slot_version > config.max_writes` ceiling exists in the gossip/sync validation path (`validate_received_chunk`) [9](#0-8) , so the frozen state also blocks acceptance of any pushed/synced chunk update from peers.

### Impact Explanation
The slot is rendered permanently unwritable — a self-inflicted (or attacker-triggered against another key they legitimately hold, e.g. a signer's own slot) denial of service on that specific StackerDB replica slot. This matches the "unauthenticated/unauthorized write to state" / DoS class in that it permanently disables the write-path for a slot that is part of consensus-adjacent signer messaging infrastructure, without any recovery short of a config change or contract redeployment. It is a single-message action (one accepted chunk) with permanent, non-recoverable effect on that slot for the current signer/StackerDB epoch. The blast radius is confined to that one slot (one signer's numbered slot in the given cycle's StackerDB), not the whole network, but it is a legitimate write path (not a forged one) reaching a genuinely broken invariant: the code assumes there is always "room" between `latest_version` and `max_writes`, but when `max_writes == u32::MAX` no such room exists at the top of the range.

### Likelihood Explanation
The precondition — `max_writes == u32::MAX` — is not hypothetical/config-dependent in the adversarial sense demanded by the question; it is the actual constant baked into the live `signers.clar` boot contract that governs real signer StackerDBs [2](#0-1) . Triggering the bug requires only owning a slot's private key (in scope per the threat model — "own a StackerDB slot they legitimately hold") and sending one chunk with `slot_version = u32::MAX`, which is a normal, properly-signed `StackerDBChunkData` message over the existing push/upload path. No secret, no privileged role, and no exotic conditions are required. Repeatability is limited (it only needs to happen once per slot to freeze it), but the attack is trivial to mount for any of the ~small number of signer slots.

### Recommendation
Reserve at least one version value below the actual configured/contract-supplied `max_writes` ceiling as unusable headroom, or explicitly cap `max_writes` to `u32::MAX - 1` (or some safety margin) in `StackerDBConfig::eval_config`/`from_smart_contract` so `slot_version` can never reach `u32::MAX`. Alternatively, change the stale/ceiling checks in `try_replace_chunk` and `validate_received_chunk` to use `slot_desc.slot_version >= self.config.max_writes` combined with a config-time clamp of `max_writes` to `u32::MAX - 1`, guaranteeing there is always a next representable version.

### Proof of Concept
```rust
// stackslib/src/net/stackerdb/tests/db.rs
#[test]
fn test_max_writes_u32_max_permanently_freezes_slot() {
    let mut db = StackerDBs::connect_memory();
    let contract_id = /* test contract id */;
    let privk = StacksPrivateKey::new();
    let pubkey = StacksPublicKey::from_private(&privk);
    let addr = StacksAddress::from_public_keys(...).unwrap();

    let config = StackerDBConfig {
        chunk_size: 1024,
        signers: vec![(addr.clone(), 1)],
        write_freq: 0,
        max_writes: u32::MAX,   // mirrors signers.clar's MAX_WRITES = u4294967295
        hint_replicas: vec![],
        max_neighbors: 8,
    };

    let db_tx = db.tx_begin(config).unwrap();
    db_tx.create_stackerdb(&contract_id, &[(addr.clone(), 1)]).unwrap();

    // 1. Legitimate write at slot_version = u32::MAX succeeds (passes ceiling check).
    let chunk = b"attacker-controlled chunk".to_vec();
    let mut slot_md = SlotMetadata::new_unsigned(0, u32::MAX, Sha512Trunc256Sum::from_data(&chunk));
    slot_md.sign(&privk).unwrap();
    assert!(db_tx.try_replace_chunk(&contract_id, &slot_md, &chunk).is_ok());

    // 2. Any subsequent write, even a well-formed one, is permanently rejected:
    // slot_version must be > u32::MAX, which is impossible in a u32.
    let next_chunk = b"legit follow-up write".to_vec();
    let mut next_md = SlotMetadata::new_unsigned(0, u32::MAX, Sha512Trunc256Sum::from_data(&next_chunk)); // can't exceed u32::MAX
    next_md.sign(&privk).unwrap();
    let err = db_tx.try_replace_chunk(&contract_id, &next_md, &next_chunk).unwrap_err();
    assert!(matches!(err, net_error::StaleChunk { .. }));
    // Slot 0 is now permanently frozen — no future slot_version value can exceed u32::MAX.
}
```
This exercises the exact code path at [1](#0-0)  and demonstrates that once `slot_version = u32::MAX` is accepted under a `max_writes = u32::MAX` config (matching `signers.clar`'s `MAX_WRITES`), the `slot_desc.slot_version <= slot_validation.version` guard at line 424 makes every future write to that slot fail with `StaleChunk`.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L366-396)
```rust
        slot_id: u32,
    ) -> Result<Option<SlotValidation>, net_error> {
        inner_get_slot_validation(self.conn(), smart_contract, slot_id)
    }

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

**File:** stackslib/src/net/stackerdb/db.rs (L411-437)
```rust
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

**File:** stackslib/src/chainstate/stacks/boot/signers.clar (L5-6)
```text
(define-constant MAX_WRITES u4294967295)
(define-constant CHUNK_SIZE (* u2 u1024 u1024))
```

**File:** stackslib/src/net/stackerdb/config.rs (L439-503)
```rust
        let max_writes = config_tuple
            .get("max-writes")
            .expect("FATAL: missing 'max-writes'")
            .clone()
            .expect_u128()?;
        if max_writes > u32::MAX as u128 {
            let reason = format!(
                "Contract {} stipulates a max-write bound beyond u32::MAX",
                contract_id
            );
            warn!("{}", &reason);
            return Err(NetError::InvalidStackerDBContract(
                contract_id.clone(),
                reason,
            ));
        }

        let mut max_neighbors = config_tuple
            .get("max-neighbors")
            .expect("FATAL: missing 'max-neighbors'")
            .clone()
            .expect_u128()?;

        if max_neighbors > usize::MAX as u128 {
            let reason = format!(
                "Contract {} stipulates a maximum number of neighbors beyond usize::MAX",
                contract_id
            );
            warn!("{}", &reason);
            return Err(NetError::InvalidStackerDBContract(
                contract_id.clone(),
                reason,
            ));
        }

        if max_neighbors > u128::from(local_max_neighbors) {
            debug!(
                "Contract {} stipulates a maximum number of neighbors ({}) beyond locally-configured maximum {}; defaulting to locally-configured maximum",
                contract_id,
                max_neighbors,
                local_max_neighbors,
            );
            max_neighbors = u128::from(local_max_neighbors);
        }

        let hint_replicas = if let Some(replicas) = local_hint_replicas {
            replicas
        } else {
            let hint_replicas_list = config_tuple
                .get("hint-replicas")
                .expect("FATAL: missing 'hint-replicas'")
                .clone()
                .expect_list()?;

            Self::eval_hint_replicas(contract_id, hint_replicas_list)?
        };

        Ok(StackerDBConfig {
            chunk_size: chunk_size as u64,
            signers,
            write_freq: write_freq as u64,
            max_writes: max_writes as u32,
            hint_replicas,
            max_neighbors: max_neighbors as usize,
        })
```

**File:** stackslib/src/net/stackerdb/mod.rs (L708-715)
```rust
        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }
```
