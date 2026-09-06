### Title
Stale StackerDB Slot Signer Retained When Reconfiguration Reports Zero Signers, Letting a Removed Signer Keep Writing Valid Chunks - (File: `stackslib/src/net/stackerdb/mod.rs`)

### Summary
`StackerDBs::create_or_reconfigure_stackerdbs` deliberately skips calling `reconfigure_stackerdb` (and thus skips resetting the on-disk `signer` field for every slot) whenever the freshly-loaded `new_config.signers` list is empty. This mirrors the MENTO bug pattern: the authoritative source of truth (the smart-contract-derived signer set) changes, but the persisted per-slot authorization state (`chunks.signer` in the StackerDB SQLite table) is left untouched, so validation continues to trust the old, now-unentitled signer.

### Finding Description
`create_or_reconfigure_stackerdbs` decides whether to update a replica's on-disk slot ownership with: [1](#0-0) 

The `reconfigure_stackerdb` DB call — which is the only code path that rewrites the `signer` column for each slot, per `StackerDBTx::reconfigure_stackerdb` — is gated by `!new_config.signers.is_empty()`: [2](#0-1) 

If the contract's signer set legitimately becomes empty for a cycle (e.g., a transient read failure that legitimately returns `vec![]` via `StackerDBConfig::from_smart_contract`, or the contract's `stackerdb-get-signer-slots` genuinely returning no entries), `new_config.signers.is_empty()` is true, so the `reconfigure_stackerdb` call that would wipe/rotate slot ownership is never invoked. The in-memory `stacker_db_configs` map is still updated to the new (empty) config, but the on-disk `chunks.signer` rows keep the *previous* signer's address.

All chunk-write authentication paths — `StackerDBTx::try_replace_chunk` (via `get_slot_validation`) and `PeerNetwork::validate_received_chunk` (via `get_slot_signer`) — read this stale `signer` column, not the in-memory `stacker_db_configs`: [3](#0-2) [4](#0-3) 

So a previously-entitled signer — who the current authoritative config says should have zero slots — can still produce a validly-signed `StackerDBChunkData`/`StackerDBPushChunkData` that passes `validate_received_chunk`/`try_replace_chunk`, gets stored, and is then rebroadcast network-wide via `Relayer::process_stacker_db_chunks` / `process_uploaded_stackerdb_chunks`: [5](#0-4) 

This is the same class of bug as the MENTO report: the entity's real entitlement (locked MENTO / current signer set) is revoked, but a persisted, unlinked piece of state (veMENTO lines / on-disk `signer` column) is not correspondingly cleared, so authorization checks against that stale state incorrectly still succeed.

### Impact Explanation
An address that has been de-registered as a StackerDB signer (analogous to the withdrawn-MENTO holder) can continue writing and having the network relay its chunks as authentic, unauthorized-write and forged-data-propagation into a shared replicated store that miners/signers rely on (e.g., `.miners`/`.signers` contracts use exactly this mechanism). This matches the "unauthenticated/unauthorized write to state or StackerDB" and "network-wide propagation of forged data" Critical impact categories, since data attributed to a revoked signer is stored and gossiped as if still valid.

### Likelihood Explanation
The trigger condition (`new_config.signers.is_empty()`) can occur any time the contract-derived signer set momentarily reports empty (including transient/legitimate empty reward-set windows) while a previous non-empty config's slots are still present in the DB from an earlier cycle. No attacker action is needed to create the window — an attacker simply needs to already be a slot signer at some point and then continue emitting valid signed chunks after their entitlement should have been revoked. This requires only sending StackerDB chunk-push messages, i.e., a normal, unprivileged network operation.

### Recommendation
Remove the `!new_config.signers.is_empty()` special case (or explicitly call `clear_stackerdb_slots`/full reconfiguration with an empty signer set) so that whenever the authoritative signer set changes — including to empty — the on-disk `signer`/`version`/`data` state for every slot is reset, mirroring how the MENTO fix recommends removing the delegate's voting lines whenever the underlying locked balance is fully withdrawn.

### Proof of Concept
1. Configure a StackerDB-backed contract with signer `A` owning slot 0; node stores `chunks.signer = A`.
2. Trigger a reconfiguration cycle where `StackerDBConfig::from_smart_contract` yields `new_config.signers == vec![]` (e.g., due to a transient contract read hiccup or an empty reward set) — `create_or_reconfigure_stackerdbs` skips `reconfigure_stackerdb` because of the `!new_config.signers.is_empty()` guard, per [6](#0-5) .
3. `A` (who per the current authoritative config should own zero slots) signs and pushes a new `StackerDBChunkData` for slot 0.
4. `PeerNetwork::validate_received_chunk` calls `get_slot_signer`, which still returns `A` from the stale `chunks` table, so the chunk validates and is stored/rebroadcast, per [4](#0-3) .

### Citations

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

**File:** stackslib/src/net/stackerdb/db.rs (L302-351)
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

**File:** stackslib/src/net/relay.rs (L2382-2453)
```rust
    /// Process newly-arrived chunks obtained from a peer stackerdb replica.
    /// Chunks that we store will be broadcast, since successful storage implies that they were new
    /// to us (and thus might be new to our neighbors)
    pub fn process_stacker_db_chunks(
        &mut self,
        rc_consensus_hash: &ConsensusHash,
        stackerdb_configs: &HashMap<QualifiedContractIdentifier, StackerDBConfig>,
        sync_results: Vec<StackerDBSyncResult>,
        event_observer: Option<&dyn StackerDBEventDispatcher>,
    ) -> Result<(), Error> {
        // sort stacker results by contract, so as to minimize the number of transactions.
        let mut sync_results_map: HashMap<QualifiedContractIdentifier, Vec<StackerDBSyncResult>> =
            HashMap::new();
        for sync_result in sync_results.into_iter() {
            if let Some(result_list) = sync_results_map.get_mut(&sync_result.contract_id) {
                result_list.push(sync_result);
            } else {
                sync_results_map.insert(sync_result.contract_id.clone(), vec![sync_result]);
            }
        }

        let mut all_events: HashMap<QualifiedContractIdentifier, Vec<StackerDBChunkData>> =
            HashMap::new();

        for (sc, sync_results) in sync_results_map.into_iter() {
            if let Some(config) = stackerdb_configs.get(&sc) {
                let tx = self.stacker_dbs.tx_begin(config.clone())?;
                for sync_result in sync_results.into_iter() {
                    for (origin, chunk) in sync_result.chunks_to_store.into_iter() {
                        let md = chunk.get_slot_metadata();
                        if let Err(e) = tx.try_replace_chunk(&sc, &md, &chunk.data) {
                            if matches!(e, Error::StaleChunk { .. }) {
                                // This is a common and expected message, so log it as a debug and with a sep message
                                // to distinguish it from other message types.
                                debug!(
                                    "Dropping stale StackerDB chunk";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            } else {
                                warn!(
                                    "Failed to store chunk for StackerDB";
                                    "stackerdb_contract_id" => %sync_result.contract_id,
                                    "slot_id" => md.slot_id,
                                    "slot_version" => md.slot_version,
                                    "num_bytes" => chunk.data.len(),
                                    "error" => %e
                                );
                            }
                            continue;
                        } else {
                            log_stored_stackerdb_chunk(&sync_result.contract_id, &chunk, &origin);
                        }

                        if let Some(event_list) = all_events.get_mut(&sync_result.contract_id) {
                            event_list.push(chunk.clone());
                        } else {
                            all_events.insert(sync_result.contract_id.clone(), vec![chunk.clone()]);
                        }

                        let msg = StacksMessageType::StackerDBPushChunk(StackerDBPushChunkData {
                            contract_id: sc.clone(),
                            rc_consensus_hash: rc_consensus_hash.clone(),
                            chunk_data: chunk,
                        });
                        if let Err(e) = self.p2p.broadcast_message(vec![], msg) {
                            warn!("Failed to broadcast StackerDB chunk: {e:?}");
                        }
                    }
```
