### Title
Stale/`noop()` StackerDB config silently replaces a valid on-disk chunk-size/write-count policy while preserving the real signer table, allowing size/write-quota bypass and network-wide propagation of oversized chunks - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`PeerNetwork::create_or_reconfigure_stackerdbs` decides whether to physically reconfigure a StackerDB replica's slot table based on comparing the newly-loaded `StackerDBConfig` against the previous one, but it decides whether to *publish* (`self.stacker_db_configs` / `new_stackerdb_configs`) the new config independently of that decision. When `StackerDBConfig::from_smart_contract` transiently fails, the node falls back to `StackerDBConfig::noop()` (empty `signers`, `chunk_size = STACKERDB_MAX_CHUNK_SIZE`, `max_writes = u32::MAX`), yet this permissive, signer-less config is still inserted into the published config map even though the on-disk slot table (real signers/limits) is left untouched.

### Finding Description
`create_or_reconfigure_stackerdbs` [1](#0-0)  computes `new_config` either from the real contract or, on any failure (`StackerDBConfig::from_smart_contract` erroring for any reason), falls back to `StackerDBConfig::noop()`: [2](#0-1) 

The reconfiguration guard is:
```
} else if (new_config != stackerdb_config && !new_config.signers.is_empty())
    || (new_config == stackerdb_config
        && new_config.signers.len() != self.get_slot_versions(&stackerdb_contract_id)?.len())
{
    self.reconfigure_stackerdb(&stackerdb_contract_id, &new_config)
}
``` [3](#0-2) 

When the config load fails and `noop()` (with `signers: vec![]`) is produced, `new_config != stackerdb_config` is true but `!new_config.signers.is_empty()` is false, so neither branch fires — the actual on-disk chunk table (created earlier via `create_stackerdb`/`reconfigure_stackerdb` with the real per-signer slot assignments) is left completely untouched. However, regardless of whether reconfiguration happened, the code unconditionally does:
```
new_stackerdb_configs.insert(stackerdb_contract_id, new_config);
``` [4](#0-3) 

This publishes the noop, signer-less, unlimited-size/unlimited-writes config as the authoritative in-memory `StackerDBConfig` for that contract via `refresh_stacker_db_configs` → `self.stacker_db_configs`: [5](#0-4) 

That published config is exactly what is used to *enforce* size/version limits on incoming chunks in `validate_received_chunk` (`config.chunk_size`, `config.max_writes`): [6](#0-5) 

and it is also passed straight into `tx_begin(config.clone())` in `process_stacker_db_chunks`/`process_pushed_stacker_db_chunks`, whose `try_replace_chunk` re-derives its `chunk_size`/`max_writes` caps from that same `self.config`: [7](#0-6) [8](#0-7) 

Crucially, `try_replace_chunk` still authenticates the signer via `get_slot_validation`, which reads the *actual, ground-truth* on-disk `signer` for that slot (unaffected by the noop config), so any signer who legitimately owns a slot under the real contract can still write. But the size and write-count ceilings they are checked against are now `STACKERDB_MAX_CHUNK_SIZE` and `u32::MAX` instead of the contract's real, smaller `chunk_size`/`max_writes`. This is exactly the pattern in the report: the "policy table" (config) governing what should be accepted diverges from the ground truth of what actually exists/should be allowed (the real per-slot table), and nothing re-derives or re-checks that equality before the write path trusts the stale policy.

### Impact Explanation
Any already-registered StackerDB slot owner (e.g., a signer in `.signers`/`.miners` or any custom StackerDB contract) can, once their node (or any relaying node whose `create_or_reconfigure_stackerdbs` hit a `from_smart_contract` failure) is running with this `noop()`-corrupted config, push chunks far larger than the contract's declared `chunk-size` and with unlimited version/write counts. `process_stacker_db_chunks`/`process_pushed_stacker_db_chunks` will accept and store such an oversized/excessive-write chunk and then unconditionally broadcast it network-wide via `self.p2p.broadcast_message`: [9](#0-8) 
This causes propagation of data that violates the StackerDB contract's declared size/quota policy to the whole network (each receiving peer's own `validate_received_chunk` will independently accept it too, since the size/version checks are policy-only, not consensus-verified against the contract on every peer). This matches "network-wide propagation of forged data" — the accepted data is not what the contract's policy authorizes, and it defeats the node-level defenses against resource-exhaustion (oversized chunks, unbounded lamport-clock growth) that `chunk_size`/`max_writes` were designed to enforce.

### Likelihood Explanation
The `noop()` fallback path is reached whenever `StackerDBConfig::from_smart_contract` errors for any reason other than the boot-contract-not-found case (any Clarity eval error, unexpected return shape, DB error, etc.) — see the `unwrap_or_else` fallback: [10](#0-9) 
This is called every time `refresh_stacker_db_configs` runs (on tip changes), so any transient failure to evaluate the StackerDB contract silently and durably (until the next successful load) widens size/write enforcement for that contract on the affected node, without requiring any admin/privileged action — just a normal legitimate slot owner sending an oversized/rapid-fire chunk while the node's config is in this state.

### Recommendation
When `StackerDBConfig::from_smart_contract` fails, do not fall back to a permissive `noop()` config for a contract that already has an existing, correctly-configured replica; instead keep serving the last-known-good config (or refuse to accept/relay chunks for that contract) until a config reload succeeds. Additionally, `validate_received_chunk` and `try_replace_chunk` should treat `noop()`/failed-load configs as "untrusted for size/write enforcement" rather than silently using its maximal defaults.

### Proof of Concept
1. Set up a StackerDB contract with a small `chunk-size` (e.g. `u1024`) and modest `max-writes` (e.g. `u10`), and let a node successfully load/create it via `create_or_reconfigure_stackerdbs`.
2. Trigger a transient failure in `StackerDBConfig::from_smart_contract` on a subsequent refresh (e.g. simulate a Clarity eval error or a chain-tip race) so that `create_or_reconfigure_stackerdbs` falls into the `unwrap_or_else(|e| ... StackerDBConfig::noop())` branch for that contract, while the contract's real signer set is non-empty (so `reconfigure_stackerdb` is skipped, per the `!new_config.signers.is_empty()` guard).
3. Observe that `self.stacker_db_configs` (as returned by `get_stacker_db_configs()`) now maps this contract to `StackerDBConfig::noop()` — `chunk_size = STACKERDB_MAX_CHUNK_SIZE`, `max_writes = u32::MAX`, `signers = []`.
4. As a legitimate existing slot owner, sign and push a chunk sized between the original `chunk-size` and `STACKERDB_MAX_CHUNK_SIZE`, at a slot version far beyond the contract's `max-writes`.
5. Observe via `PeerNetwork::validate_received_chunk` / `StackerDBTx::try_replace_chunk` that the chunk is accepted (since `get_slot_validation`'s signer check still passes against ground truth) and then broadcast network-wide by `process_stacker_db_chunks`, exceeding the contract's declared size/write-count policy.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L345-435)
```rust
    /// Create or reconfigure the supplied contracts with the appropriate stacker DB config.
    /// Returns a map of the stacker DBs and their loaded configs.
    /// Fails only if the underlying DB fails
    pub fn create_or_reconfigure_stackerdbs(
        &mut self,
        chainstate: &mut StacksChainState,
        sortdb: &SortitionDB,
        stacker_db_configs: HashMap<QualifiedContractIdentifier, StackerDBConfig>,
        connection_opts: &ConnectionOptions,
    ) -> Result<HashMap<QualifiedContractIdentifier, StackerDBConfig>, net_error> {
        let num_neighbors = connection_opts.num_neighbors;
        let existing_contract_ids = self.get_stackerdb_contract_ids()?;
        let mut new_stackerdb_configs = HashMap::new();
        let tip = SortitionDB::get_canonical_burn_chain_tip(sortdb.conn())?;

        for (stackerdb_contract_id, stackerdb_config) in stacker_db_configs.into_iter() {
            // Determine the new config for this StackerDB replica
            let new_config = if stackerdb_contract_id
                == boot_code_id(MINERS_NAME, chainstate.mainnet)
            {
                // .miners contract -- directly generate the config
                NakamotoChainState::make_miners_stackerdb_config(sortdb, &tip)
                    .map(|(config, _)| config)
                    .unwrap_or_else(|e| {
                        warn!(
                            "Failed to generate .miners StackerDB config";
                            "contract" => %stackerdb_contract_id,
                            "err" => ?e,
                        );
                        StackerDBConfig::noop()
                    })
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
            // Even if we failed to create or reconfigure the DB, we still want to keep track of them
            // so that we can attempt to create/reconfigure them again later.
            debug!("Reloaded configuration for {}", &stackerdb_contract_id);
            new_stackerdb_configs.insert(stackerdb_contract_id, new_config);
        }
        Ok(new_stackerdb_configs)
    }
```

**File:** stackslib/src/net/stackerdb/mod.rs (L649-717)
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
```

**File:** stackslib/src/net/p2p.rs (L4464-4478)
```rust
    /// Reload StackerDB configs from chainstate
    pub fn refresh_stacker_db_configs(
        &mut self,
        sortdb: &SortitionDB,
        chainstate: &mut StacksChainState,
    ) -> Result<(), net_error> {
        let stacker_db_configs = mem::replace(&mut self.stacker_db_configs, HashMap::new());
        self.stacker_db_configs = self.stackerdbs.create_or_reconfigure_stackerdbs(
            chainstate,
            sortdb,
            stacker_db_configs,
            &self.connection_opts,
        )?;
        Ok(())
    }
```

**File:** stackslib/src/net/relay.rs (L2406-2412)
```rust
        for (sc, sync_results) in sync_results_map.into_iter() {
            if let Some(config) = stackerdb_configs.get(&sc) {
                let tx = self.stacker_dbs.tx_begin(config.clone())?;
                for sync_result in sync_results.into_iter() {
                    for (origin, chunk) in sync_result.chunks_to_store.into_iter() {
                        let md = chunk.get_slot_metadata();
                        if let Err(e) = tx.try_replace_chunk(&sc, &md, &chunk.data) {
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

**File:** stackslib/src/net/stackerdb/db.rs (L398-436)
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
```
