### Title
Stale on-disk StackerDB slot-signer bindings persist and remain authoritative when a contract's new signer set is empty - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`StackerDBs::create_or_reconfigure_stackerdbs` decides whether to overwrite a replica's persisted slot ownership (`chunks.signer`) with a freshly-fetched signer list. The reconfiguration is gated so that it is skipped whenever the newly computed config has an empty `signers` list, even though the in-memory config map is still updated to that (empty) config. This creates a stale-authority window analogous to the PhiNFT1155 bug: the previously-configured signer address remains the one authorized to write/sign chunks for that slot (`get_slot_signer` reads the untouched on-disk binding), even though the authoritative source (the controlling contract, re-evaluated at the current chain tip) no longer designates that signer. [1](#0-0) 

### Finding Description
`create_or_reconfigure_stackerdbs` computes `new_config` fresh from the controlling smart contract (or falls back to `StackerDBConfig::noop()`, which has `signers: vec![]`, on any evaluation error or an intentionally empty signer set such as a non-participating reward cycle): [2](#0-1) 

It then only calls `reconfigure_stackerdb` (which rewrites the `chunks.signer` column via `StackerDBTx::reconfigure_stackerdb`) under this gate:

```
} else if (new_config != stackerdb_config && !new_config.signers.is_empty())
    || (new_config == stackerdb_config
        && new_config.signers.len()
            != self.get_slot_versions(&stackerdb_contract_id)?.len())
{
    ...
    self.reconfigure_stackerdb(&stackerdb_contract_id, &new_config) ...
}
// Even if we failed to create or reconfigure the DB, we still want to keep track of them
new_stackerdb_configs.insert(stackerdb_contract_id, new_config);
``` [3](#0-2) 

When `new_config.signers.is_empty()` (empty signer set from the contract, or `noop()` fallback on any transient eval error) and it differs from the previously stored config, neither branch of the `||` is satisfied, so `reconfigure_stackerdb` is never called — the on-disk `chunks.signer` bindings from the previous configuration are left untouched. However, `new_stackerdb_configs.insert(stackerdb_contract_id, new_config)` unconditionally stores the *new*, empty-signers config as the node's current view (returned by `refresh_stacker_db_configs`/`get_stacker_db_configs`) at `stackslib/src/net/p2p.rs:4465-4478`. [4](#0-3) 

The equality that breaks is: "who the contract currently designates as the authoritative signer for slot N" vs. "who the persisted `chunks` row says is the signer for slot N." `validate_received_chunk` — the function that authenticates every downloaded or pushed StackerDB chunk — resolves authorization purely from the persisted DB row via `get_slot_signer`, not from the in-memory `StackerDBConfig.signers` list: [5](#0-4) 

So even though the contract has moved to an empty (or different) signer set for that reward cycle/slot, the node keeps validating and accepting chunks signed by the old, now-unauthorized signer key, and (per `Relayer::process_stacker_db_chunks`) rebroadcasts any newly-accepted chunk to the rest of the network: [6](#0-5) 

This mirrors the external report precisely: a privileged identity (fee destination / StackerDB signer) is changed at the authoritative source, but a locally cached/stored binding is never refreshed, so the *old* identity continues to be treated as valid indefinitely.

### Impact Explanation
An old signer whose signing key was never compromised by an attacker (they already legitimately possess it from a prior reward cycle/config) can continue to have their StackerDB writes accepted and propagated across the network after the contract has revoked their signer status (e.g. reward cycle changed to `has_participation = false`, producing an empty `stackerdb_list`, per `stackslib/src/chainstate/nakamoto/signer_set.rs:578-601`). This is an unauthorized/stale write persisted at all replicas that hit the same skip condition, and forged/outdated data continues to be relayed as if legitimate — matching "network-wide propagation of forged data" / "unauthenticated ... write to state or StackerDB". It also silently defeats the goal of key rotation for compromised signer keys, similar to the PhiNFT1155 stale `protocolFeeDestination`.

### Likelihood Explanation
The empty-signers condition is not a rare edge case: it is explicitly produced by legitimate protocol logic whenever `has_participation` is false for a reward cycle (`signers.clar` / `signer_set.rs`), and also transiently by any contract-evaluation error causing `StackerDBConfig::from_smart_contract` to fall back to `noop()`. The guard exists specifically to avoid a previously-fixed shrink bug (#5142), but as written it also suppresses legitimate revocation of signer authority whenever the new signer list is empty, so the condition is reachable under normal operational scenarios, not just via attacker-crafted transient errors.

### Recommendation
Do not conflate "skip reconfigure to avoid transient noop() wipes" with "skip reconfigure when the contract legitimately returns an empty signer set." Distinguish an evaluation failure (keep old config/signers) from an authoritative empty signer set (which should still cause `reconfigure_stackerdb` to clear/invalidate the old slot-signer bindings). At minimum, when `new_config.signers.is_empty()` but the fetch from the contract succeeded (not a fallback `noop()`), the stale bindings should be cleared so `get_slot_signer` cannot keep authorizing writes from a revoked signer.

### Proof of Concept
1. Configure `.signers-0-XX` (or `.signers-1-XX`) contract state such that reward cycle `N` has signer `S` bound to slot `0` via `stackerdb-set-signer-slots`. Node calls `create_or_reconfigure_stackerdbs`; `new_config.signers = [(S, 1)]`, differs from prior `noop()` config, is non-empty → `reconfigure_stackerdb` runs, binding slot 0's `chunks.signer` to `S`.
2. At the boundary of reward cycle `N+2` (same contract ID reused, per `cycle-mod`), the reward-set calculation determines `has_participation = false`, so `update_signers` in `stackslib/src/chainstate/nakamoto/signer_set.rs` submits an empty `stackerdb_list` to the contract; `stackerdb-get-signer-slots-page` now returns `[]`.
3. Node calls `refresh_stacker_db_configs` → `create_or_reconfigure_stackerdbs`; `new_config.signers = []`. Condition `(new_config != stackerdb_config && !new_config.signers.is_empty())` is false (empty), and `(new_config == stackerdb_config && ...)` is false (configs differ) → `reconfigure_stackerdb` is skipped. `new_stackerdb_configs` still stores the empty-signers config as current.
4. Signer `S` (who still holds their private key from cycle `N`, and is no longer an authorized signer) crafts and signs a `StackerDBChunkData` for slot 0 and pushes it via `StackerDBPushChunk`.
5. `handle_unsolicited_StackerDBPushChunk` → `validate_received_chunk` calls `self.stackerdbs.get_slot_signer(contract_id, 0)`, which still returns `S` (never cleared in step 3) → signature verifies → chunk is accepted and stored, then rebroadcast via `Relayer::process_stacker_db_chunks`/`p2p.broadcast_message`, propagating the now-unauthorized signer's data across the network.

### Citations

**File:** stackslib/src/net/stackerdb/mod.rs (L360-405)
```rust
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
```

**File:** stackslib/src/net/stackerdb/mod.rs (L406-435)
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
