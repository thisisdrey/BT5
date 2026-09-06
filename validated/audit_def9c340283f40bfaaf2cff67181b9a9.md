### Title
Stale StackerDB signer authorization after signer-set reconfiguration fails to apply — ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`StackerDBs::create_or_reconfigure_stackerdbs` decides whether to push a freshly-computed signer-set configuration into the on-disk StackerDB replica (the table that ultimately authorizes which `StacksAddress` may write to which slot). It skips the reconfiguration whenever the newly loaded config has an empty signer list, even though the config is recognized as having changed from what is currently stored. This mirrors the Arcade `setMultiplier` bug class: a config value changes, but the enforcement state derived from it (there, voting power; here, which signer key is authorized to write a given StackerDB slot) is not resynchronized.

### Finding Description
`create_or_reconfigure_stackerdbs` recomputes `new_config` for every tracked StackerDB contract on each pass (e.g. `.signers-N-M`, `.miners`) and only calls `reconfigure_stackerdb` under this condition: [1](#0-0) 

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

If `new_config != stackerdb_config` (the signer set genuinely changed, e.g. a reward-cycle rotation dropped or replaced a signer) **but** `new_config.signers` happens to be empty (which occurs whenever `StackerDBConfig::from_smart_contract` fails to parse/verify a valid signer list and the code falls back to `StackerDBConfig::noop()`), the `reconfigure_stackerdb` call is skipped entirely: [2](#0-1) 

Despite skipping the on-disk reconfiguration, the function still records `new_config` as the node's authoritative view of the contract's config: [3](#0-2) 

The actual per-slot authorization used at write time, however, is not derived from `new_config` — it comes from the `chunks` table populated by the last successful `reconfigure_stackerdb`/`create_stackerdb` call: [4](#0-3) 

That table backs `get_slot_signer`/`get_slot_validation`, which `try_replace_chunk` and `validate_received_chunk` use to authenticate incoming chunk writes: [5](#0-4) [6](#0-5) 

So the "equality" that should hold — *signer authorized to write slot X == signer currently assigned slot X per the latest on-chain config* — can be broken: the previous signer set (including an address that has since been removed/rotated out) remains valid against the local replica even though the node itself has already accepted a newer config value that logically supersedes it.

### Impact Explanation
A signer who has been removed or replaced in a new reward cycle's `.signers` (or `.miners`) contract can continue to have chunks bearing their old key accepted and stored/relayed by any peer that hits this stale-skip path, because that peer's on-disk slot ownership was never updated to match the new roster. This is an unauthorized write to StackerDB state and causes propagation of data from a party no longer entitled to write it, which other nodes may treat as authentic signer/miner state.

### Likelihood Explanation
This requires the config-loading path to yield an empty signer list while `new_config != stackerdb_config` is true — e.g., a transient contract-read/parse failure at a reward-cycle boundary (falling back to `StackerDBConfig::noop()`), which is plausible during signer-set rotation but not attacker-triggerable on demand from a remote unprivileged peer; it depends on the local node's own config-loading behavior at a particular boundary. This lowers likelihood relative to a directly-triggerable remote bug, though the fault path itself is concretely reachable and requires no privileged access or secret key.

### Recommendation
Do not silently skip reconfiguration when `new_config != stackerdb_config`. If the newly loaded config is empty due to a load failure, either (a) retain the previous, verified config (do not overwrite `new_stackerdb_configs` with the failed/empty one) so the stored config and the enforced slot ownership stay consistent, or (b) treat a genuinely-changed-but-unparseable config as a hard error that prevents chunk writes/reads for that contract until a valid config is obtained, rather than continuing to honor the previous signer roster under a newly-recorded (different) config value.

### Proof of Concept
1. Node tracks `.signers-N-M` with `stackerdb_config` containing signer `S_old`.
2. At reward-cycle rollover, `StackerDBConfig::from_smart_contract` transiently fails to load the new roster (e.g., contract read/verification hiccup) and returns `StackerDBConfig::noop()` (empty `signers`).
3. `create_or_reconfigure_stackerdbs` sees `new_config != stackerdb_config` but `new_config.signers.is_empty()`, so `reconfigure_stackerdb` is **not** called; the on-disk `chunks` table still lists `S_old` as slot owner.
4. `new_stackerdb_configs` now records the empty `new_config` as current, per [3](#0-2) .
5. `S_old`, though no longer part of the intended new signer set, signs and pushes a chunk; `try_replace_chunk`/`validate_received_chunk` authenticate it against the stale `chunks` table entry and accept/store/relay it as valid, per [5](#0-4) .

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

**File:** stackslib/src/net/stackerdb/mod.rs (L429-434)
```rust
            // Even if we failed to create or reconfigure the DB, we still want to keep track of them
            // so that we can attempt to create/reconfigure them again later.
            debug!("Reloaded configuration for {}", &stackerdb_contract_id);
            new_stackerdb_configs.insert(stackerdb_contract_id, new_config);
        }
        Ok(new_stackerdb_configs)
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

**File:** stackslib/src/net/stackerdb/db.rs (L302-327)
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
```

**File:** stackslib/src/net/stackerdb/db.rs (L411-423)
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
```
