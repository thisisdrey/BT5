### Title
Stale per-slot signer bindings survive contract-driven signer revocation, letting removed StackerDB writers keep writing/propagating chunks indefinitely - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`StackerDBs::create_or_reconfigure_stackerdbs` refuses to apply a reconfiguration whenever the freshly-loaded `StackerDBConfig` has an empty `signers` list, even though it still records that empty config as the new "current" config. Because `reconfigure_stackerdb` is the only code path that updates the per-slot signer binding stored in the `chunks` table, skipping it means a StackerDB replica keeps honoring write authorizations for **removed** signers after the controlling smart contract has revoked them — the same "authorization residue" pattern as the Gitea webhook advisory (a permission grant that is not tied to a live re-check and keeps functioning after the grantor revokes access).

### Finding Description
Slot ownership (which `StacksAddress` may write to which `slot_id`) is persisted in the `chunks` table's `signer` column and enforced purely from that stored value: [1](#0-0) 

`try_replace_chunk` looks up `get_slot_validation` (the DB-stored signer) and accepts the write if the chunk's signature recovers to that address — it never consults the live on-chain signer list at write time.

The only mechanism that updates this stored signer binding when the controlling contract's signer set changes is `reconfigure_stackerdb`: [2](#0-1) 

which is invoked from the `StackerDBs`-level wrapper: [3](#0-2) 

But the caller that decides *whether* to invoke it, `create_or_reconfigure_stackerdbs`, explicitly gates the call on the new config's signer list being non-empty: [4](#0-3) 

```
} else if (new_config != stackerdb_config && !new_config.signers.is_empty())
    || (new_config == stackerdb_config
        && new_config.signers.len()
            != self.get_slot_versions(&stackerdb_contract_id)?.len())
{
    // only reconfigure if the config has changed
    ...
    self.reconfigure_stackerdb(&stackerdb_contract_id, &new_config)
}
...
new_stackerdb_configs.insert(stackerdb_contract_id, new_config);
```

If the controlling contract legitimately drives its signer list to empty (e.g., the last authorized writer is removed / a single-signer DB has its slot count set to `0`), `new_config.signers.is_empty()` is true, so `reconfigure_stackerdb` is **never called** — the on-disk `chunks.signer` column for that contract's slots is left untouched, still pointing at the old, now-revoked signer address. Meanwhile the in-memory `new_stackerdb_configs` map is updated to the new (empty) config regardless, so the discrepancy between "on-chain authorization" (none) and "on-disk enforcement" (old signer still bound) persists across reward cycles/config reloads with no recovery path other than a config that becomes non-empty again with a *different* signer for that slot.

Because `validate_received_chunk`/`try_replace_chunk` authenticate purely against the stale `chunks.signer` value: [5](#0-4) 

the previously-authorized (now revoked) signer's chunks continue to validate, get stored, and get gossiped to the rest of the StackerDB-replicating network via the standard push/pull relay path, exactly mirroring the Gitea bug's "grant outlives revocation, and the stale grant keeps producing externally-visible effects."

### Impact Explanation
This breaks the equality "currently on-chain authorized signer" == "signer the node will accept writes from." A revoked writer's chunk is accepted as valid data by the local replica and relayed to peers as legitimate StackerDB content, i.e. unauthorized write to StackerDB state plus network-wide propagation of stale/forged-authority data — squarely in the "Critical" impact bucket described by the rules (unauthenticated/unauthorized write to state or StackerDB; network-wide propagation of forged data).

### Likelihood Explanation
The trigger condition — the *entire* signer list for a given StackerDB contract transitioning to empty in one reload — is a real, reachable state for any custom/application StackerDB contract that removes its sole/last signer (or momentarily returns an empty list), and is explicitly anticipated by the code's `noop()`/`is_empty()` guard (added to defend against reload failures, per the comment in `StackerDBConfig::noop`). No node operator action or privileged role is required; the previously-authorized party simply keeps signing and submitting chunks with their retained private key after being removed from the contract's signer set.

### Recommendation
Do not conflate "config failed to load" with "config legitimately went to zero signers." Track load failures separately (e.g., `Option<StackerDBConfig>`/explicit error flag) so `create_or_reconfigure_stackerdbs` can still call `reconfigure_stackerdb` (which safely resets/drops slot data whenever a slot's bound signer changes) when the *authoritative* on-chain signer list is empty, instead of silently preserving stale signer bindings. At minimum, when transitioning to an empty signer set, proactively call `clear_stackerdb_slots`/`reconfigure_stackerdb` with the empty list so no slot retains a stale signer binding.

### Proof of Concept
1. Deploy a StackerDB-controlling contract with a single signer `S` owning slot 0 (`num-slots: 1`); node replicates it, `S` writes chunk v1 (accepted, `chunks.signer = S`).
2. Update the contract so `stackerdb-get-signer-slots` now returns an empty list (revoking `S`, e.g. `num-slots: 0` removal) — this is a legitimate on-chain state transition, no privileged node action needed.
3. On the next config reload, `StackerDBConfig::from_smart_contract` returns `signers: vec![]`; in `create_or_reconfigure_stackerdbs` the branch at [6](#0-5)  is false (`new_config.signers.is_empty()`), so `reconfigure_stackerdb` is skipped — slot 0's `chunks.signer` column still equals `S`.
4. `S` (now unauthorized on-chain) signs and submits chunk v2 for slot 0; `try_replace_chunk`/`validate_received_chunk` still validate it against the stale stored signer `S` and accept + relay it to peers, confirming write access persisted past revocation.

### Citations

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

**File:** stackslib/src/net/stackerdb/mod.rs (L325-343)
```rust
    /// Reconfigure a StackerDB.
    /// Fails only if the underlying DB fails
    fn reconfigure_stackerdb(
        &mut self,
        stackerdb_contract_id: &QualifiedContractIdentifier,
        new_config: &StackerDBConfig,
    ) -> Result<(), db_error> {
        debug!("Reconfiguring StackerDB {stackerdb_contract_id}...");
        let tx = self.tx_begin(new_config.clone())?;
        tx.reconfigure_stackerdb(stackerdb_contract_id, &new_config.signers)
            .unwrap_or_else(|e| {
                warn!(
                    "Failed to reconfigure StackerDB replica {}: {:?}",
                    stackerdb_contract_id, &e
                );
            });
        tx.commit()?;
        Ok(())
    }
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
