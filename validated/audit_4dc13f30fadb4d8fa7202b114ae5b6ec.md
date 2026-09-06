## Title
Transient StackerDB config-eval failure silently downgrades write enforcement to `noop()` limits, allowing unauthorized/unbounded writes - (File: stackslib/src/net/stackerdb/mod.rs)

### Summary
This is an analog of the "stale exchangeRate on/off toggle" bug: a piece of node state (the StackerDB *enforcement config*) is supposed to track the canonical, contract-derived configuration, but under a specific state transition (an evaluation failure of the controlling contract) the in-memory enforcement config silently becomes the permissive `StackerDBConfig::noop()` while the on-disk replica (signers/slots) is left untouched. This breaks the equality "enforced config == canonical contract config" and can allow writes that the canonical contract config would have rejected.

### Finding Description
`StackerDBs::create_or_reconfigure_stackerdbs` computes `new_config` for every configured contract, either from `StackerDBConfig::from_smart_contract(..)` or, on any error (network/DB/contract-eval failure), from `StackerDBConfig::noop()`: [1](#0-0) 

`noop()` sets `write_freq: 0` and `max_writes: u32::MAX` (chunk_size is capped, per an in-code comment noting a prior, now-fixed, issue with that specific field): [2](#0-1) 

Critically, regardless of whether the DB replica was actually reconfigured, the possibly-`noop()` `new_config` is unconditionally inserted into the returned config map that becomes the node's live enforcement config (`self.stacker_db_configs` in `PeerNetwork`): [3](#0-2) 

That returned map is installed via `refresh_stacker_db_configs`: [4](#0-3) 

The resulting `StackerDBConfig` (with `max_writes = u32::MAX`, `write_freq = 0`) is exactly what is used to validate incoming pushed/relayed chunks (`validate_received_chunk`, which checks `data.slot_version > config.max_writes`): [5](#0-4) 

and it is also the config passed into `tx_begin` for HTTP-posted chunks (`try_replace_chunk`'s `max_writes` check), which is the on-chain-derived cap on total slot writes: [6](#0-5) [7](#0-6) 

The important asymmetry, exactly mirroring M-12: while the config is "off" (i.e., a transient eval failure occurs), the code correctly avoids destroying existing on-disk slot data/signers (skips `reconfigure_stackerdb` when `new_config.signers.is_empty()`) — but it does *not* avoid installing the loose enforcement parameters (`max_writes`, `write_freq`) into the live config used for every subsequent write validation until the next successful refresh. In the original bug, `exchangeRate` was frozen and became stale/too-low when a vault went from off→on; here, the analogous quantity (`max_writes`/`write_freq`, the enforcement thresholds) becomes stale in the *wrong direction* — replaced by permissive defaults — precisely when the authoritative (from-contract) configuration temporarily cannot be evaluated, and remains that way for all writers (including a still-valid, previously-authorized signer) until the next successful `refresh_stacker_db_configs` call picks up the real contract config again.

### Impact Explanation
This falls under "unauthenticated/unauthorized write to state" for StackerDB: a legitimate slot signer (who is still authorized to write to the slot per the unaffected on-disk `signer` assignment) can push writes at unlimited version/rate (`max_writes = u32::MAX`, `write_freq = 0`) for as long as the node's in-memory config remains `noop()`-derived, i.e., until the contract can be evaluated successfully again. This effectively bypasses the on-chain-configured write-count and write-frequency caps that are supposed to bound StackerDB abuse (e.g., signer message spam, replay-flood of chunks) — a cap that is part of the protocol's DoS/anti-spam design for StackerDB replicas. Because the same permissive config is also used to accept relayed `StackerDBChunk`/`StackerDBPushChunk` messages network-wide (via `validate_received_chunk`), a burst of writes can be gossiped and accepted by any peer that is simultaneously in this degraded state.

### Likelihood Explanation
The trigger condition is any `Err` returned by `StackerDBConfig::from_smart_contract` — this includes ordinary transient conditions such as a Clarity read-only evaluation failure (e.g., cost-budget related), or any `NetError` surfaced from chain-state/DB access during config refresh. Because `create_or_reconfigure_stackerdbs`/`refresh_stacker_db_configs` is invoked routinely on every new canonical Stacks tip (via `refresh_stackerdb` in the relayer loop), any contract that is momentarily hard to evaluate (e.g., because of expensive analysis, or a boundary condition in `eval_config`) will cause every node processing that tip to transiently fall back to `noop()` enforcement at the same time — making this a plausible, remotely-triggerable (via causing the relevant tip/contract state to be expensive/erroring) network-wide window of degraded enforcement, not merely a local corner case.

### Recommendation
When `StackerDBConfig::from_smart_contract` fails and the fallback `noop()` is used only to avoid destructive reconfiguration of the physical DB, do not also install `noop()`'s permissive `write_freq`/`max_writes` into the live enforcement map. Instead, on eval failure, keep re-using the previous *known-good* `stackerdb_config` (the one passed in) for enforcement purposes, and only ever fall back to `noop()` for control-plane operations (slot creation/shrink) — mirroring the already-applied fix for `chunk_size` (capping it at `STACKERDB_MAX_CHUNK_SIZE`) but applied to `max_writes` and `write_freq` as well, e.g. by defaulting the fallback to the last successfully-fetched config rather than to `noop()`.

### Proof of Concept
1. A node's `refresh_stacker_db_configs` runs on a new canonical Stacks tip whose contract evaluation for a given `.signers`/StackerDB contract raises a transient `NetError` (this can be induced by making the contract's `stackerdb-get-config`/signer-slot evaluation exceed available read-only execution budget, or by any local/DB hiccup during `SortitionDB`/`StacksChainState` access at that tip).
2. `StackerDBConfig::from_smart_contract` returns `Err`, so `new_config = StackerDBConfig::noop()` (`write_freq = 0`, `max_writes = u32::MAX`).
3. Because `new_config.signers.is_empty()`, the `reconfigure_stackerdb` branch is skipped — on-disk slots/signers remain from the last good config — but `new_stackerdb_configs.insert(contract_id, new_config)` still stores the noop config.
4. `refresh_stacker_db_configs` installs this map as `self.stacker_db_configs`.
5. Until the next successful refresh (i.e., until the contract can be evaluated cleanly again), any currently-valid slot signer can submit `POST /v2/stackerdb/.../chunks` (or push `StackerDBPushChunk`) with an arbitrarily high `slot_version` and at unlimited frequency, and `try_replace_chunk`/`validate_received_chunk` will accept it because `max_writes = u32::MAX` and no rate limit is enforced, whereas the actual contract-configured `max_writes`/`write_freq` would have rejected it.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L249-267)
```rust
impl StackerDBConfig {
    /// Config that does nothing
    pub fn noop() -> StackerDBConfig {
        StackerDBConfig {
            // Cap the chunk size at the protocol-wide maximum rather than u64::MAX.
            // `noop()` is used as a fallback whenever a replica's real config can't be
            // loaded (see `create_or_reconfigure_stackerdbs`), and it can transiently
            // overwrite a good in-memory config on a failed refresh. Since the DB slots
            // persist independently of the config, writes can still land on existing
            // slots while `noop()` is active, so its chunk-size limit must never be
            // looser than `STACKERDB_MAX_CHUNK_SIZE`.
            chunk_size: STACKERDB_MAX_CHUNK_SIZE as u64,
            write_freq: 0,
            max_writes: u32::MAX,
            hint_replicas: vec![],
            max_neighbors: 8,
            signers: vec![],
        }
    }
```

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

**File:** stackslib/src/net/stackerdb/mod.rs (L406-434)
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
            // Even if we failed to create or reconfigure the DB, we still want to keep track of them
            // so that we can attempt to create/reconfigure them again later.
            debug!("Reloaded configuration for {}", &stackerdb_contract_id);
            new_stackerdb_configs.insert(stackerdb_contract_id, new_config);
        }
        Ok(new_stackerdb_configs)
```

**File:** stackslib/src/net/stackerdb/mod.rs (L699-717)
```rust
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

**File:** stackslib/src/net/stackerdb/db.rs (L424-436)
```rust
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

**File:** stackslib/src/net/api/poststackerdbchunk.rs (L179-201)
```rust
        let ack_resp =
            node.with_node_state(|network, _sortdb, _chainstate, _mempool, _rpc_args| {
                let tx = if let Ok(tx) = network.stackerdbs_tx_begin(&contract_identifier) {
                    tx
                } else {
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                };
                if let Err(_e) = tx.get_stackerdb_id(&contract_identifier) {
                    // shouldn't be necessary (this is checked against the peer network's configured DBs),
                    // but you never know.
                    return Err(StacksHttpResponse::new_error(
                        &preamble,
                        &HttpNotFound::new("StackerDB not found".to_string()),
                    ));
                }
                if let Err(e) = tx.try_replace_chunk(
                    &contract_identifier,
                    &stackerdb_chunk.get_slot_metadata(),
                    &stackerdb_chunk.data,
                ) {
```
