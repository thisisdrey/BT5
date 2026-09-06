### Title
Reward-cycle StackerDB reconfiguration resets slot Lamport clock to zero, enabling replay of previously-broadcast (stale) chunks as canonical current data - (File: stackslib/src/net/stackerdb/db.rs)

### Summary
`StackerDBTx::reconfigure_stackerdb` re-derives slot ownership from the controlling smart contract once per reward cycle and, whenever a slot's owning `signer` address changes, wipes that slot's stored version/data/hash/signature back to `NO_VERSION` (0) [1](#0-0) . If the same signer address is later re-assigned that same `slot_id` in a subsequent reward cycle (a normal, non-adversarial occurrence in signer-set rotation), the slot's Lamport clock baseline has been reset to 0 even though that signer previously wrote chunks at much higher versions. `try_replace_chunk` only compares the incoming `slot_version` against the currently stored `slot_validation.version` [2](#0-1) , so any old, validly-signed `StackerDBChunkData` for that signer/slot — which was already broadcast on the (public, unauthenticated) StackerDB gossip protocol and can be captured by any observer — will pass the freshness check and be accepted and re-propagated as if it were fresh, current-cycle data. This mirrors the "offboard() clears all nonces" bug class: a periodic reset of a replay-prevention counter (the Lamport version) permits previously-used signed messages to be validly resubmitted.

### Finding Description
StackerDB slot ownership and quota are controlled by a smart contract queried "once per reward cycle in order to configure the database" [3](#0-2) . That configuration flows into `reconfigure_stackerdb`, which for each `(slot_id, principal)` pair checks the existing `SlotValidation.signer`; if it equals the new principal, nothing changes (version preserved), but if it differs, the code does an `INSERT OR REPLACE` that sets `version = NO_VERSION (0)`, `data = []`, `data_hash = 0x00..`, `signature = empty` [4](#0-3) .

The only replay defense for chunk writes is `try_replace_chunk`, which requires: chunk size ≤ config, a valid signature over `(slot_id, slot_version, data_hash)` recovering to the slot's currently-recorded signer, `slot_version > slot_validation.version`, and `slot_version <= config.max_writes` [5](#0-4) . None of these checks bind the chunk to a specific reward cycle or to a monotonically-increasing counter that survives a `signer` reassignment — they only compare against whatever `slot_validation.version` currently sits in the row.

Consequently, the sequence:
1. Signer `S` owns `slot_id = k` in reward cycle `N`, and validly signs/publishes several chunks up to `slot_version = V` (V observed on the wire by any peer, since StackerDB traffic is unauthenticated gossip open to any listener).
2. In reward cycle `N+1`, the contract reassigns `slot_id = k` to a different signer `T` (a routine rotation event, not requiring any attacker privilege) — `reconfigure_stackerdb` resets `slot_id = k` to `version = 0`.
3. In reward cycle `N+2`, `S` is reassigned back to `slot_id = k` (plausible under normal stacking/signer-set churn) — because the signer at this point is once again `S`, but the version was already zeroed in step 2, the slot's stored version stays at 0.
4. Any remote, unprivileged party who captured `S`'s old chunk from step 1 (data + still-valid signature over `slot_id`, old `slot_version`, and `data_hash`) can now POST it to any replica. It passes signature verification (still signed by `S`, who is again the registered signer), passes `slot_version(V) > slot_validation.version(0)`, and is accepted and re-gossiped by `handle_unsolicited_StackerDBPushChunk` / the sync protocol to the rest of the network [6](#0-5) .

The equality that should hold — "a chunk accepted as canonical/current for signer `S` was actually authored and intended by `S` for the current context" — is broken because the version counter, the sole anti-replay mechanism, is reset by an ownership-churn event unrelated to `S`'s own signing intent.

### Impact Explanation
This lets stale, previously-retired StackerDB content (e.g., an old signer message, vote, or block-commit artifact from a prior reward cycle) be re-admitted as the current/canonical chunk for a slot and propagated network-wide via the StackerDB sync/push-chunk gossip path, without the original signer taking any action and without the replaying party possessing any private key. This is a case of non-canonical (stale) state being served and relayed as canonical current state across the network, matching the High/Critical impact classes described (network-wide propagation of forged/stale data; serving non-canonical state as canonical). Depending on what application is layered on the StackerDB (e.g., the `.signers` StackerDB used by stacks-signer), resurrecting an old signed message as "current" could mislead consumers of that StackerDB into acting on stale application state.

### Likelihood Explanation
The reset condition (`existing_validation.signer != principal`) is triggered by ordinary reward-cycle-driven signer-set churn, not by any attacker action — it requires no privileged access, admin role, or possession of a private key. The only requirements are: (a) a signer address is rotated out of and later back into the same `slot_id`, and (b) an old chunk from that signer was observed on the public gossip network. Both conditions are plausible in the normal operation of Stacks' signer-set rotation across reward cycles, especially when the same set of signer addresses/slot layouts recur.

### Recommendation
Do not reset the Lamport version to a value below any version the signer has ever legitimately used at that `slot_id`. Options analogous to the original report's suggestions:
- Track a monotonically increasing "epoch"/reconfiguration nonce per stackerdb, and bind chunk signatures to `(slot_id, epoch, slot_version)` instead of `(slot_id, slot_version)` alone, so a chunk signed in an older epoch cannot satisfy the freshness check in a newer epoch.
- When reassigning a slot to a signer who previously owned it, do not reset its version below the historical maximum ever recorded for that signer/slot pair (e.g., persist a "signer last used version" table keyed by signer address rather than by slot_id alone).
- Alternatively, never actually reuse a numeric `slot_id` for a different signer without incrementing a globally unique generation counter incorporated into the signed digest.

### Proof of Concept
Not independently executable without the full node/test harness, but the logical PoC (consistent with the existing test `test_reconfigure_stackerdb` which already demonstrates the version-reset behavior [7](#0-6) ) is:
1. Create a StackerDB, assign `slot_id=0` to signer `S`; have `S` sign and store `chunk_v5` at `slot_version=5`.
2. Call `reconfigure_stackerdb` to reassign `slot_id=0` to a different signer `T` — this resets slot 0's stored version to 0 [8](#0-7) .
3. Call `reconfigure_stackerdb` again, reassigning `slot_id=0` back to `S` — signer matches, so no explicit reset occurs, but the version remains at 0 from step 2.
4. Re-submit the previously captured `chunk_v5` (same bytes/signature as step 1) via `try_replace_chunk` / the `POST /v2/stackerdb/...` RPC path. It is accepted because `5 > 0`, even though it is stale content already superseded before the reconfiguration.

### Citations

**File:** stackslib/src/net/stackerdb/db.rs (L302-346)
```rust
    pub fn reconfigure_stackerdb(
        &self,
        smart_contract: &QualifiedContractIdentifier,
        slots: &[(StacksAddress, u32)],
    ) -> Result<(), net_error> {
        let stackerdb_id = self.get_stackerdb_id(smart_contract)?;
        let mut total_slots_read = 0u32;
        for (principal, slot_count) in slots.iter() {
            total_slots_read =
                total_slots_read
                    .checked_add(*slot_count)
                    .ok_or(net_error::OverflowError(
                        "Slot count exceeeds u32::MAX".to_string(),
                    ))?;
            let slots_before_principal = total_slots_read - slot_count;
            for cur_principal_slot in 0..*slot_count {
                let slot_id = slots_before_principal + cur_principal_slot;
                if let Some(existing_validation) =
                    self.get_slot_validation(smart_contract, slot_id)?
                {
                    // this slot already exists.
                    if existing_validation.signer == *principal {
                        // no change
                        continue;
                    }
                }

                debug!("Reset slot {} of {}", slot_id, smart_contract);

                // new slot, or existing slot with a different signer
                let qry = "INSERT OR REPLACE INTO chunks (stackerdb_id,signer,slot_id,version,write_time,data,data_hash,signature) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)";
                let mut stmt = self.sql_tx.prepare(qry)?;
                let args = params![
                    stackerdb_id,
                    principal.to_string(),
                    slot_id,
                    NO_VERSION,
                    0,
                    vec![],
                    Sha512Trunc256Sum([0u8; 32]),
                    MessageSignature::empty(),
                ];

                stmt.execute(args)?;
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

**File:** stackslib/src/net/stackerdb/mod.rs (L59-62)
```rust
/// The smart contract to which a StackerDB is bound controls how many slots the DB has, who can
/// write to which slots (identified by public key hash), how big a slot is, and how often a
/// slot can be written to (in wall-clock time).  This smart contract is queried once per reward cycle
/// in order to configure the database.
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-718)
```rust
    pub fn validate_received_chunk(
        &self,
        smart_contract_id: &QualifiedContractIdentifier,
        config: &StackerDBConfig,
        data: &StackerDBChunkData,
        expected_versions: &[u32],
    ) -> Result<bool, net_error> {
        // validate -- must not exceed this replica's configured chunk size.
        if (data.data.len() as u64) > config.chunk_size {
            info!(
                "Received StackerDBChunk for {} ID {}, which is oversized: {} bytes (max {} bytes)",
                smart_contract_id,
                data.slot_id,
                data.data.len(),
                config.chunk_size
            );
            return Ok(false);
        }

        // validate -- must be a valid chunk
        let Some(expected_version) = expected_versions.get(data.slot_id as usize) else {
            info!(
                "Received StackerDBChunk for {} ID {}, which is too big ({})",
                smart_contract_id,
                data.slot_id,
                expected_versions.len()
            );
            return Ok(false);
        };

        // validate -- must be signed by the expected author
        let addr = match self
            .stackerdbs
            .get_slot_signer(smart_contract_id, data.slot_id)?
        {
            Some(addr) => addr,
            None => {
                return Ok(false);
            }
        };

        let slot_metadata = data.get_slot_metadata();
        if !slot_metadata.verify(&addr)? {
            info!(
                "StackerDBChunk for {} ID {} is not signed by {}",
                smart_contract_id, data.slot_id, &addr
            );
            return Ok(false);
        }

        // validate -- must be the current or newer version
        if data.slot_version < *expected_version {
            info!(
                "Received StackerDBChunk for {} ID {} version {}, which is stale (expected {})",
                smart_contract_id, data.slot_id, data.slot_version, *expected_version
            );
            return Ok(false);
        }

        // validate -- must not exceed max writes
        if data.slot_version > config.max_writes {
            info!(
                "Write count exceeded for StackerDBChunk for {} ID {} version {} (max is {})",
                smart_contract_id, data.slot_id, data.slot_version, config.max_writes
            );
            return Ok(false);
        }

        Ok(true)
    }
```

**File:** stackslib/src/net/stackerdb/tests/db.rs (L601-649)
```rust
    // reconfigure
    tx.reconfigure_stackerdb(
        &sc,
        &reconfigured_addrs
            .into_iter()
            .map(|addr| (addr, 1))
            .collect::<Vec<_>>(),
    )
    .unwrap();

    tx.commit().unwrap();

    for (i, pk) in new_pks.iter().enumerate() {
        if i < 5 {
            // first five are unchanged
            let chunk_data = StackerDBChunkData {
                slot_id: i as u32,
                slot_version: 1,
                sig: MessageSignature::empty(),
                data: vec![i as u8; 128],
            };

            let slot_metadata = db.get_slot_metadata(&sc, i as u32).unwrap().unwrap();
            let chunk = db.get_latest_chunk(&sc, i as u32).unwrap().unwrap();

            assert_eq!(initial_metadata[i].0, slot_metadata);
            assert_eq!(initial_metadata[i].1.data, chunk);
        } else if i < 10 {
            // next five are wiped
            let slot_metadata = db.get_slot_metadata(&sc, i as u32).unwrap().unwrap();
            assert_eq!(slot_metadata.slot_id, i as u32);
            assert_eq!(slot_metadata.slot_version, 0);
            assert_eq!(slot_metadata.data_hash, Sha512Trunc256Sum([0x00; 32]));
            assert_eq!(slot_metadata.signature, MessageSignature::empty());

            let chunk = db.get_latest_chunk(&sc, i as u32).unwrap().unwrap();
            assert!(chunk.is_empty());
        } else {
            // final five are new
            let slot_metadata = db.get_slot_metadata(&sc, i as u32).unwrap().unwrap();
            assert_eq!(slot_metadata.slot_id, i as u32);
            assert_eq!(slot_metadata.slot_version, 0);
            assert_eq!(slot_metadata.data_hash, Sha512Trunc256Sum([0x00; 32]));
            assert_eq!(slot_metadata.signature, MessageSignature::empty());

            let chunk = db.get_latest_chunk(&sc, i as u32).unwrap().unwrap();
            assert!(chunk.is_empty());
        }
    }
```
