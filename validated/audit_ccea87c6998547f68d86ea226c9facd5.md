### Title
Gas key delete+recreate resets nonce floor to a block-height-only value, enabling DelegateAction replay - ([File: runtime/runtime/src/actions.rs], [File: runtime/runtime/src/access_keys.rs])

### Summary
`validate_delegate_action_key` reads the "already used" watermark for a `TransactionNonce::GasKeyNonce` fresh from storage via `get_gas_key_nonce` on every call, and compares it only against `delegate_nonce.nonce()` and the current block's ceiling. When a gas key is deleted and a new gas key is added with the *same* public key, every nonce row is reseeded to `initial_nonce_value(block_height)`, a value that depends only on the current block height, not on any previous "high water mark" for that key. If the recreation happens at the same block height at which a high nonce was previously consumed, the reseeded floor can be lower than that already-consumed nonce, making a previously executed `DelegateAction` payload valid again.

### Finding Description
- `validate_delegate_action_key` fetches `current_nonce` per index straight from the trie via `get_gas_key_nonce(state_update, sender_id, public_key, nonce_index)` [1](#0-0) , and only checks `delegate_nonce.nonce() <= current_nonce` and `delegate_nonce.nonce() >= block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [2](#0-1) .
- `add_gas_key` (invoked from `AddKeyAction` handling) reseeds *every* nonce row for the key to `initial_nonce_value(block_height)` unconditionally, with no memory of any prior generation of the same public key: [3](#0-2) .
- `initial_nonce_value` is `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, purely a function of the current block height [4](#0-3) .
- `delete_gas_key` fully removes all nonce rows for the old key generation with no retained residue that could raise the new floor [5](#0-4) .
- Because the DelegateAction's own upper bound (`upper_bound = apply_state.block_height * MULTIPLIER`) grows with block height while a delete+recreate at that same height resets the floor back to `(H-1)*MULTIPLIER`, any nonce `V` that was legitimately consumed in the range `[(H-1)*MULTIPLIER, H*MULTIPLIER)` becomes valid again once the key is deleted and an identically-keyed gas key is recreated at height `H`.
- Unlike ordinary `SignedTransaction`s, whose replay window is bounded by a short block-hash freshness check, a `DelegateAction`'s replay window is bounded only by `max_block_height`, a field the signer (attacker) chooses at signing time and can set arbitrarily far in the future — so the previously captured DelegateAction bytes remain resubmittable long after the original execution.
- The attacker can force the drain, `DeleteKey`, and `AddKey` to land at the identical `apply_state.block_height` by batching them as actions of a single transaction (`Action::DelegateV2(drain)`, `Action::DeleteKey(pk)`, `Action::AddKey(pk, new_gas_key)`), which are applied sequentially within one receipt/apply pass, guaranteeing the same `block_height` context for all three steps.
- No existing check ties the reseeded nonce floor to the highest nonce ever consumed by any previous generation of that public key on that account, so the "exactly once" guarantee for `DelegateAction` execution is not preserved across key regeneration.

### Impact Explanation
This is a double-spend/replay vulnerability: a previously signed and executed `DelegateAction` (e.g., wrapping a `Transfer`, `FunctionCall`, or other action) can be re-executed a second time after the attacker deletes and recreates their own gas key with the same public key at the same block height, without needing any new signature. Since the attacker fully controls both the drain and the delete/recreate transaction (all self-relayed, no privileged access needed), the concrete impact is uncontrolled re-execution of a delegated action — for example duplicating a transfer, effectively creating value out of thin air relative to the intended single execution, matching the "double-spend/replay" bounty category.

### Likelihood Explanation
Preconditions are all attacker-controlled and require no privileged role: a funded account, gas keys stabilized (v85+), and the ability to relay one's own meta-transactions. The attack is deterministic if the drain DelegateAction, `DeleteKey`, and `AddKey` are batched into a single transaction's action list (guaranteeing identical `apply_state.block_height` for all three), and the replay submission can occur at any later block since the newly reseeded floor stays fixed and the ceiling only grows. The main engineering effort is choosing a nonce `V` in the reachable range and constructing the batched transaction; this is inexpensive and repeatable for every gas key the attacker owns.

### Recommendation
Do not derive the gas-key nonce floor purely from `block_height` when a `DeleteKey`+`AddKey` sequence recreates the *same* public key. Options: (1) track a per-account monotonic "key generation" or "highest nonce ever used" watermark that survives key deletion for a given public key and use `max(initial_nonce_value(block_height), previous_watermark)` when reseeding; or (2) forbid/ignore reuse of a deleted public key within some safety window; or (3) tie the DelegateAction's nonce space to something that cannot be reset independently of consumption history, e.g., persisting the last-known-high nonce per `(account_id, public_key)` in storage across deletion (not removed by `delete_gas_key`) and reseeding new keys strictly above it.

### Proof of Concept
Test-loop / runtime unit test plan:
1. At block height `H`, add a gas key with `num_nonces = K` to an attacker-controlled account (self-relayer).
2. Sign and execute a `DelegateActionV2` using `nonce_index = 0` with `nonce = V` chosen close to `H*ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (but valid), wrapping an inner `Transfer` action; assert it succeeds and the receiver balance increases.
3. In the same block height `H` (batch into a single transaction alongside the drain, or via test-loop control of block height), submit `DeleteKey(pk)` followed by `AddKey(pk, gas_key_full_access(K))` reusing the identical public key.
4. Assert numerically that `get_gas_key_nonce(..., 0) == (H-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER <= V`.
5. Resubmit the exact `signed_delegate_action` bytes from step 2 (same nonce `V`, same signature) as a new meta-transaction; assert it succeeds again (`FinalExecutionStatus::SuccessValue`) and the receiver balance increases a second time by the same transfer amount, violating exactly-once execution.

### Citations

**File:** runtime/runtime/src/actions.rs (L619-628)
```rust
            let current_nonce =
                get_gas_key_nonce(state_update, sender_id, public_key, nonce_index)?.ok_or_else(
                    || {
                        StorageError::StorageInconsistentState(format!(
                            "gas key nonce row missing for {} {} at in-range index {nonce_index} (num_nonces {})",
                            sender_id, public_key, gas_key_info.num_nonces,
                        ))
                    },
                )?;
            (current_nonce, DelegateNonceUpdate::GasKey { nonce_index })
```

**File:** runtime/runtime/src/actions.rs (L632-650)
```rust
    if delegate_nonce.nonce() <= current_nonce {
        result.result = Err(ActionErrorKind::DelegateActionInvalidNonce {
            delegate_nonce: delegate_nonce.nonce(),
            ak_nonce: current_nonce,
        }
        .into());
        return Ok(());
    }

    let upper_bound = apply_state.block_height
        * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER;
    if delegate_nonce.nonce() >= upper_bound {
        result.result = Err(ActionErrorKind::DelegateActionNonceTooLarge {
            delegate_nonce: delegate_nonce.nonce(),
            upper_bound,
        }
        .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L114-126)
```rust
    let num_nonces = gas_key_info.num_nonces as usize;
    for i in 0..gas_key_info.num_nonces {
        remove_gas_key_nonce(state_update, account_id.clone(), public_key.clone(), i);
    }
    let nonce_key_len = gas_key_nonce_key_len(account_id, &public_key.into());
    let nonce_remove_compute = storage_removes_compute(
        &config.wasm_config.ext_costs,
        num_nonces,
        nonce_key_len * num_nonces,
        AccessKey::NONCE_VALUE_LEN * num_nonces,
    );
    result.compute_usage = safe_add_compute(result.compute_usage, nonce_remove_compute)?;
    remove_access_key(state_update, account_id.clone(), public_key.clone());
```

**File:** runtime/runtime/src/access_keys.rs (L209-214)
```rust
    // Set up nonces for gas key
    let num_nonces = gas_key_info.num_nonces;
    let nonce = initial_nonce_value(block_height);
    for i in 0..num_nonces {
        set_gas_key_nonce(state_update, account_id.clone(), public_key.clone(), i, nonce);
    }
```
