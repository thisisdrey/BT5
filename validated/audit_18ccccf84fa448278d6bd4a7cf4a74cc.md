## Title
Stale StackerDB signer authorization is never revoked when a contract-computed signer set becomes empty, letting deauthorized signers keep write access - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`StackerDBs::create_or_reconfigure_stackerdbs` only calls `reconfigure_stackerdb` (which is the only code path that resets/clears a slot's signer and data) when the freshly-computed config's `signers` list is non-empty. When a StackerDB-controlling contract legitimately transitions to an *empty* signer list (e.g. a no-participation reward cycle for the `.signers-N-xxx` contracts), the node silently skips reconfiguration, leaving the previous, now-unauthorized signer(s) recorded as the valid slot owner in the `chunks`/`slot_validation` table. This mirrors the reported Solidity bug class: a state transition that revokes authorization (here, "no eligible signers this cycle") is never enforced at the storage layer because the code that would clear/zero the stale entitlement is gated behind a check that fails exactly when it matters most.

### Finding Description
`create_or_reconfigure_stackerdbs` computes `new_config` from the on-chain contract every time it runs, then decides whether to call `reconfigure_stackerdb`: [1](#0-0) 

The gating condition is:
```
(new_config != stackerdb_config && !new_config.signers.is_empty())
    || (new_config == stackerdb_config && new_config.signers.len() != self.get_slot_versions(...)?.len())
```
If the contract legitimately reports zero signers this cycle (`new_config.signers` is empty) while the previously stored config had signers, `new_config != stackerdb_config` is true but `!new_config.signers.is_empty()` is false, so the whole first disjunct is false. The second disjunct also fails because `new_config == stackerdb_config` is false. Consequently `reconfigure_stackerdb` is **never invoked**, and the code falls through to just caching `new_config` in `new_stackerdb_configs` without touching the on-disk `chunks`/`slot_validation` rows.

`reconfigure_stackerdb` in `stackslib/src/net/stackerdb/db.rs` is the only function that resets a slot's signer/data when ownership changes: [2](#0-1) 
Because it is skipped, the previous signer's `slot_validation.signer` entry (and their last-written chunk) remains intact in the database.

Downstream, both `try_replace_chunk` and `validate_received_chunk` authenticate incoming writes purely against this stored `slot_validation`/`get_slot_signer` state, not against the freshly-computed (now-empty) authorized set: [3](#0-2) [4](#0-3) 

The `.signers` boot contract legitimately produces an empty signer/stackerdb list whenever a reward cycle has no participation: [5](#0-4) 

Since every honest node runs the identical deterministic logic against the same chain state, all nodes independently retain the same stale, now-unauthorized signer key as "the" valid slot owner - this is not a transient single-node inconsistency but a network-wide, consensus-reproducible failure to revoke write authorization.

### Impact Explanation
A signer who is no longer part of the authorized signer set (e.g., because their cycle had zero participation, or their permission was otherwise revoked in a way that results in an empty parsed signer list) retains the ability to submit chunk writes that are accepted as authentic by every node running this code, because the local slot-authorization record was never cleared. These chunks are then relayed/gossiped across the network like any other legitimate signer message (see `StackerDBChunksEvent`/`SignerEvent` consumption in `libsigner/src/events.rs` and `stacks-node/src/nakamoto_node/stackerdb_listener.rs`), i.e., unauthorized writes to StackerDB state that propagate network-wide as if canonical. This matches the "unauthenticated/unauthorized write to state or StackerDB" / "network-wide propagation of forged data" impact tier.

### Likelihood Explanation
No attacker action beyond normal, permitted use of a previously-valid signer key is required. The empty-signers transition happens naturally whenever a reward cycle has zero participation (a state explicitly modeled and handled in `signers.clar`/`signer_set.rs`), so the vulnerable code path is reachable through ordinary chain operation, not a contrived edge case requiring privileged access.

### Recommendation
Remove the `!new_config.signers.is_empty()` guard (or explicitly handle the empty-signers case by still calling `reconfigure_stackerdb`/clearing slots) so that a legitimate transition to zero signers actually revokes write authorization and wipes stale chunk data, mirroring how any other signer-set change is handled. Additionally, add a regression test that asserts a StackerDB replica's signer authorization is cleared once the controlling contract reports an empty signer set for a cycle.

### Proof of Concept
1. Configure a `.signers-0-xxx`-style StackerDB contract; reward cycle `N` has participation, so `signers.clar`'s `stackerdb-get-signer-slots-page` returns `[A]`. The node calls `create_or_reconfigure_stackerdbs`, creating a slot with `slot_validation.signer == A`.
2. Reward cycle `N+1` has zero participation; `update_signers` writes an empty `stackerdb-signer-slots` list for that cycle, per `signer_set.rs` (`stackerdb_list = if !has_participation { vec![] } ...`).
3. On the next call to `create_or_reconfigure_stackerdbs`, `StackerDBConfig::from_smart_contract` parses this empty list into `new_config.signers = []`. The reconfigure condition evaluates to `false` (as shown above), so `reconfigure_stackerdb` is skipped; `slot_validation.signer == A` remains in the DB.
4. Signer `A` (now unauthorized for cycle `N+1`) signs and pushes a chunk via `StackerDBPushChunkData`. `validate_received_chunk`/`try_replace_chunk` looks up `get_slot_signer`, finds the stale `A`, verifies `A`'s signature successfully, and accepts the write.
5. The chunk is stored and relayed to peers exactly as legitimate signer traffic, even though the currently valid on-chain configuration authorizes zero signers for this cycle.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L414-428)
```rust
            } else if (new_config != stackerdb_config && !new_config.signers.is_empty())
                || (new_config == stackerdb_config
                    && new_config.signers.len()
                        != self.get_slot_versions(&stackerdb_contract_id)?.len())
            {
                // only reconfigure if the config has changed
                // (that second check on the length is needed in case the node is a victim of
                // #5142, which was a bug whereby a stackerdb could never shrink)
                if let Err(e) = self.reconfigure_stackerdb(&stackerdb_contract_id, &new_config) {
                    warn!(
                        "Failed to create or reconfigure StackerDB {stackerdb_contract_id}: DB error {:?}",
                        &e
                    );
                }
            }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L679-697)
```rust
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
```

**File:** stackslib/src/net/stackerdb/db.rs (L298-351)
```rust
    /// Update a database's storage slots, e.g. from new configuration state in its smart contract.
    /// Chunk data for slots that no longer exist will be dropped.
    /// Newly-created slots will be instantiated with empty data.
    /// If the address for a slot changes, then its data will be dropped.
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
        }
        debug!("Shrink {} to {} slots", smart_contract, total_slots_read);
        self.shrink_stackerdb(stackerdb_id, total_slots_read)?;
        Ok(())
    }
```

**File:** stackslib/src/net/stackerdb/db.rs (L398-423)
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
```

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L577-601)
```rust
        let sender_addr = PrincipalData::from(boot::boot_code_addr(is_mainnet));
        let stackerdb_list = if !has_participation {
            vec![]
        } else {
            signers
                .iter()
                .map(|signer| {
                    let signer_hash = Hash160::from_data(&signer.signing_key);
                    let signing_address = StacksAddress::p2pkh_from_hash(is_mainnet, signer_hash);
                    let tuple_data = TupleData::from_data(vec![
                        (
                            ClarityName::from_literal("signer"),
                            Value::Principal(PrincipalData::from(signing_address)),
                        ),
                        (ClarityName::from_literal("num-slots"), Value::UInt(1)),
                    ])
                    .map_err(|e| {
                        ChainstateError::Expects(format!(
                            "Failed to create tuple for stackerdb entry: {e}"
                        ))
                    })?;
                    Ok::<Value, ChainstateError>(Value::Tuple(tuple_data))
                })
                .collect::<Result<Vec<_>, _>>()?
        };
```
