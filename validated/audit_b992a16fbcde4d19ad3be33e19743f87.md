### Title
Revoked StackerDB signer key retains write authorization when the on-chain signer list becomes empty - ([File: stackslib/src/net/stackerdb/mod.rs])

### Summary
`StackerDBs::create_or_reconfigure_stackerdbs` deliberately skips wiping/reconfiguring a local StackerDB replica whenever the freshly-fetched authoritative config reports an **empty** signer list, in order to avoid a prior bug (#5142) where transient contract-call failures (which fall back to `StackerDBConfig::noop()`) would wipe legitimate data. This "fail-safe" guard, however, means that when a signer's authorization is legitimately revoked such that the current authoritative signer set becomes empty (e.g., no-participation reward cycle, or contract logic reporting zero signers), the previously-provisioned slot→signer bindings in the local `chunks` table are never invalidated, and the old (deauthorized) signer's key remains valid for writing to that StackerDB.

### Finding Description
`StackerDBs::create_or_reconfigure_stackerdbs` decides whether to call `reconfigure_stackerdb` (which rewrites the slot→signer ownership rows used for chunk-signature authorization) based on: [1](#0-0) 

```rust
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

The `!new_config.signers.is_empty()` clause means: if the newly-computed authoritative config has zero signers (whether from a genuine on-chain state, such as `has_participation = false` producing an empty `stackerdb-set-signer-slots` call as seen in `update_signers` [2](#0-1) , or from a transient RPC/contract-eval failure that falls back to `StackerDBConfig::noop()`), the node will **not** call `reconfigure_stackerdb`. The on-disk `chunks` table (which stores the `signer` principal per slot, consulted by `get_slot_validation`/`get_slot_signer`) is left untouched, meaning the previous authorized signer set continues to be treated as valid.

This directly breaks the intended equality "currently-authorized signer == signer allowed to write" the same way the Mattermost bug broke "active user == valid token holder": revocation of authorization at the source of truth (the reward-cycle/contract state) does not propagate to the local enforcement point (`try_replace_chunk`'s signature check against `slot_validation.signer`): [3](#0-2) 

and [4](#0-3) 

Both paths derive the "valid signer" strictly from whatever the local slot-validation table currently holds — which the reconfigure-skip guard can leave stale indefinitely once the authoritative list becomes empty.

### Impact Explanation
If exploitable in a genuine "signers dropped to empty" scenario (as opposed to only transient RPC failures), a remote, unprivileged peer who still possesses a previously-valid signer private key (e.g., a signer removed from the active set, or a signer for a reward cycle with zero participation) could continue to push signed `StackerDBChunkData`/`StackerDBPushChunkData` messages that the network will accept and relay as authentic, since `try_replace_chunk`/`validate_received_chunk` will validate the signature against the stale, un-revoked slot-owner recorded locally. This is an unauthorized write to network state (StackerDB) by an entity whose authorization should have already been revoked, which the rules classify as Critical-severity impact ("unauthenticated/unauthorized write to state or StackerDB").

### Likelihood Explanation
The likelihood depends on how often a StackerDB's authoritative signer set can legitimately become fully empty for the same contract mid-lifetime (as opposed to being immediately superseded by a full reconfiguration with a non-empty new set). This appears most plausible around reward-cycle boundaries where `has_participation` is false, or for custom StackerDB contracts that report zero signers transiently. I could not fully confirm from the available code whether an empty signer list is a common/reachable production condition for the `.signers-N-M` StackerDB contracts versus a rare edge case; further investigation of the reward-cycle transition logic and how `.signers` contract slot configuration interacts with `create_or_reconfigure_stackerdbs` over consecutive cycles would be needed to establish a concrete reproduction sequence.

### Recommendation
Distinguish between "empty config due to a transient fetch/eval failure" (which should indeed avoid destructive reconfiguration, per the #5142 fix) and "empty config due to a genuine on-chain signer-list change" (which should still invalidate/clear existing slot ownership so stale keys cannot continue writing). Concretely: only suppress reconfiguration when the config-fetch itself errored (i.e., track a distinct "config load failed" flag rather than inferring failure from `signers.is_empty()`), and always reconfigure/clear stale signer bindings when a successfully-fetched authoritative config legitimately reports an empty (or reduced) signer set.

### Proof of Concept
Not independently reproducible from the available code/index alone; a full PoC would require constructing a StackerDB contract/reward-cycle sequence that causes `StackerDBConfig::from_smart_contract` (or `make_miners_stackerdb_config`) to legitimately return a config with a non-empty `signers` list, followed by a subsequent legitimate transition to an empty `signers` list, and then observing via `stackslib/src/net/stackerdb/tests/*` harnesses that a chunk signed by the previously-valid (now revoked) key is still accepted by `try_replace_chunk` after the transition. I was not able to fully verify from the indexed code whether such a legitimate empty-to-non-empty(-to-empty) transition is reachable in practice versus always immediately overwritten by a subsequent non-empty reconfiguration; this would need to be validated in a running node/testnet environment.

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

**File:** stackslib/src/chainstate/nakamoto/signer_set.rs (L576-601)
```rust
    ) -> Result<Vec<StacksTransactionEvent>, ChainstateError> {
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
