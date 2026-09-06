### Title
Stale StackerDB signer authorization persists across contract reconfiguration when the freshly-evaluated signer set is empty - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
`PeerNetwork::refresh_stacker_db_configs` / `StackerDBs::create_or_reconfigure_stackerdbs` only pushes a new signer set into the on-disk StackerDB replica when the newly evaluated config is non-empty. If `StackerDBConfig::from_smart_contract` fails or transiently returns an empty signer list, the persisted `chunks.signer` column (the actual authorization record used at chunk-write time) is left untouched, so a signer who has since lost write authority in the controlling contract continues to be treated as the valid owner of the slot. This mirrors the "PGP Key Revocation Not Considered" analog: authorization is checked against a locally cached credential rather than being re-derived at each verification, so revocation of that credential silently has no effect.

### Finding Description
`StackerDBTx::try_replace_chunk` and `PeerNetwork::validate_received_chunk` both authenticate incoming chunks by comparing the chunk's recovered signer against the `signer` column stored in the local `chunks` table (`get_slot_signer` / `get_slot_validation`), *not* against a live re-evaluation of the controlling smart contract: [1](#0-0) 

That stored `signer` value is only updated by `reconfigure_stackerdb`, which rewrites a slot's row whenever the persisted signer differs from the freshly-computed one: [2](#0-1) 

However, the caller that decides *whether* to invoke `reconfigure_stackerdb` gates it behind a non-empty check on the newly loaded config: [3](#0-2) 

```
} else if (new_config != stackerdb_config && !new_config.signers.is_empty())
    || (new_config == stackerdb_config
        && new_config.signers.len()
            != self.get_slot_versions(&stackerdb_contract_id)?.len())
{
    // only reconfigure if the config has changed
    ...
}
```

`new_config` is produced by `StackerDBConfig::from_smart_contract`, which falls back to `StackerDBConfig::noop()` (empty `signers`) on any read-only Clarity evaluation error: [4](#0-3) 

Because the `!new_config.signers.is_empty()` guard suppresses `reconfigure_stackerdb` whenever the freshly computed signer list is empty — whether from a genuine contract change to zero signers or from a transient/incidental evaluation failure — the on-disk `chunks.signer` mapping is never refreshed to reflect a legitimate change (including removal) of a signer's authorization. Meanwhile `new_config` (with the possibly-empty/degraded signer list) is still installed into `self.stacker_db_configs` via `new_stackerdb_configs.insert(...)`, so the in-memory "current" config and the authoritative on-disk write-authorization record can diverge indefinitely. A signer whose write authority was revoked (removed from the contract's signer-slot list) keeps their old slot's signed chunks accepted by `validate_received_chunk`/`try_replace_chunk` as long as this divergence persists, because those paths trust the stale `signer` column rather than re-deriving current authorization from chain state at verification time.

This is the direct structural analog of the PGP-advisory's core complaint: authorization is bound to a previously-copied credential (here, a DB row written at the last successful reconfiguration) instead of being re-validated against the current source of truth (here, the smart contract's live signer-slot list) at the moment of use.

### Impact Explanation
An attacker who once held write authority for a StackerDB slot (e.g., a since-rotated or since-removed Nakamoto signer/miner) can continue to have forged/stale chunks accepted and gossiped by nodes whose local reconfiguration was skipped due to this empty-signers guard (e.g. after a transient contract-read error, epoch/tip race, or a genuine but degenerate 0-signer intermediate state). Accepted chunks are then replicated network-wide via `handle_unsolicited_StackerDBPushChunk`/sync broadcast, allowing propagation of unauthorized/forged data under a StackerDB contract that node operators believe reflects current signer authorization — an unauthorized write to state that is then propagated as if canonical.

### Likelihood Explanation
Triggering requires only a transient failure of `StackerDBConfig::from_smart_contract` (any Clarity-eval error path returns `noop()`/empty signers) coinciding with, or following, a legitimate signer-set change in the controlling contract. This is plausible during normal chain-tip transitions, contract upgrades, or brief node/DB inconsistency, and does not require attacker control over consensus or another party's key — only that the attacker still possesses a previously-valid StackerDB signing key for the slot in question.

### Recommendation
Remove the `!new_config.signers.is_empty()` short-circuit (or replace it with an explicit, intentional "genuinely zero signers" signal from the contract) so that any detected change in the evaluated signer set — including transient failures that degrade to an empty set — either forces a reconfiguration/wipe of the slot's authorization, or is treated as a hard error that blocks further chunk acceptance for that contract until a successful re-evaluation succeeds, rather than silently retaining the previous authorization state.

### Proof of Concept
1. Configure a StackerDB contract with signer S1 owning slot 0; let the node successfully reconfigure and record `signer = S1` in `chunks`.
2. Update the contract to remove S1 from the signer-slot list (revoke S1's authority) and simultaneously arrange (or wait) for `StackerDBConfig::from_smart_contract` to hit any error path returning `StackerDBConfig::noop()` on the affected node (e.g. via a transient chain-tip/epoch read race) — `new_config.signers` is now empty.
3. Because `!new_config.signers.is_empty()` is false, `reconfigure_stackerdb` is skipped in `create_or_reconfigure_stackerdbs`; the on-disk `chunks.signer` for slot 0 remains S1.
4. S1 (now unauthorized per the contract) submits a chunk signed with its old key via `StackerDBPushChunk`/HTTP POST; `validate_received_chunk`/`try_replace_chunk` still finds `get_slot_signer == S1` and accepts and relays the chunk, despite S1's write authority having been revoked in the source-of-truth contract.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L376-405)
```rust
            } else {
                // attempt to load the config from the contract itself
                StackerDBConfig::from_smart_contract(
                    chainstate,
                    sortdb,
                    &stackerdb_contract_id,
                    num_neighbors,
                    connection_opts
                        .stackerdb_hint_replicas
                        .get(&stackerdb_contract_id)
                        .cloned(),
                )
                .unwrap_or_else(|e| {
                    if matches!(e, net_error::NoSuchStackerDB(_)) && stackerdb_contract_id.is_boot()
                    {
                        debug!(
                            "Failed to load StackerDB config";
                            "contract" => %stackerdb_contract_id,
                            "err" => ?e,
                        );
                    } else {
                        warn!(
                            "Failed to load StackerDB config";
                            "contract" => %stackerdb_contract_id,
                            "err" => ?e,
                        );
                    }
                    StackerDBConfig::noop()
                })
            };
```

**File:** stackslib/src/net/stackerdb/mod.rs (L406-428)
```rust
            // Create the StackerDB replica if it does not exist already
            if !existing_contract_ids.contains(&stackerdb_contract_id) {
                if let Err(e) = self.create_stackerdb(&stackerdb_contract_id, &new_config) {
                    warn!(
                        "Failed to create or reconfigure StackerDB {stackerdb_contract_id}: DB error {:?}",
                        &e
                    );
                }
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

**File:** stackslib/src/net/stackerdb/db.rs (L298-327)
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
```
