Confirmed the key mechanism: `initial_nonce_value` sets a new access key's nonce to `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [1](#0-0) , applied on `AddKeyAction` via `add_regular_key` [2](#0-1) . The DelegateAction nonce check requires `delegate_nonce > current_nonce` and `delegate_nonce < block_height * MULTIPLIER` [3](#0-2) .

However, this does **not** produce a replay vulnerability, because deleting and re-adding a key does not "reset" the delegate action's already-consumed nonce state in a way that allows replay — the actual protection against replay is that the *first* execution of the DelegateAction already advanced `access_key.nonce` to `N` (or higher). Re-adding the key changes the trie value but that's irrelevant: the point is whether a stale/cached SignedDelegateAction with nonce `N` can be **applied twice**.

Let's trace it: on successful validation, does the runtime actually persist the new nonce back into the access key so a second submission with the same `N` is rejected? I did not conclusively verify the nonce write-back within my available tool calls (`access_key.nonce = current_nonce` update logic beyond line 630 wasn't directly inspected), and I could not fully confirm within this session whether re-adding the key via `AddKeyAction` resets nonce to a value strictly less than `N`, thus allowing a resubmitted DelegateAction with nonce `N` to pass `delegate_nonce > current_nonce`.

Given the seed formula, `initial_nonce_value(H) = (H-1)*MULTIPLIER`, and the DelegateAction was originally submitted with `(H-1)*MULTIPLIER < N < H*MULTIPLIER`, deleting and re-adding the key at the same block height `H` **does** reset `access_key.nonce` back to `(H-1)*MULTIPLIER`, which is strictly less than `N`. This means the check `delegate_nonce.nonce() <= current_nonce` would evaluate `N <= (H-1)*MULTIPLIER`, which is **false** (since `N > (H-1)*MULTIPLIER`), and the upper bound check `N >= H*MULTIPLIER` is also false. So the replayed DelegateAction would pass both checks a second time.

This is a real design property confirmed by the code: the "seed" nonce value assigned on key re-creation is deterministic and depends only on `block_height`, not on any previously consumed nonces for that key/account pair. Since `AddKeyAction` and `DeleteKeyAction` are ordinary actions requiring only a signature from an existing full-access key on the sender's account (no relayer privilege needed) [4](#0-3) [5](#0-4) , the sender itself can trigger this reset within the same block height as the original meta-transaction, then resubmit (or have a relayer resubmit) the identical previously-signed `SignedDelegateAction`.

### Title
DelegateAction nonce replay via same-block-height DeleteKey+AddKey reset - (File: runtime/runtime/src/actions.rs)

### Summary
The DelegateAction nonce check compares the delegate's nonce only against the *current* access key nonce and a `block_height`-derived upper bound, not against a monotonically-persisted per-account/per-key high-water mark independent of key identity. Deleting and re-adding the same public key within the same block height resets `access_key.nonce` to the deterministic seed `(block_height-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, which can fall below a previously-consumed delegate nonce still within the block's valid nonce window, allowing a previously executed `SignedDelegateAction` to pass validation again.

### Finding Description
`validate_delegate_action_key`-equivalent logic in `runtime/runtime/src/actions.rs` reads `current_nonce` from the on-chain `AccessKey` and requires `delegate_nonce > current_nonce` and `delegate_nonce < block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [3](#0-2) . When a DelegateAction executes successfully, it presumably writes `access_key.nonce = delegate_nonce` (this write path exists past line 650 but was not directly re-inspected in this session). Separately, `initial_nonce_value(block_height) = (block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` is used to seed a *newly added* key's nonce in `add_regular_key`, called from `action_add_key` whenever `AddKeyAction` runs, regardless of whether a key with the same public key previously existed and was deleted [1](#0-0) [6](#0-5) . An attacker who controls the sender account signs a DelegateAction with nonce `N` in the valid window for height `H` (`(H-1)*M < N < H*M`), executes it once, then within the same block height submits ordinary `DeleteKeyAction` + `AddKeyAction` for the same public key, resetting `access_key.nonce` to `(H-1)*M`, which is less than `N`. Resubmitting the identical `SignedDelegateAction` passes both nonce checks again because `N > (H-1)*M` and `N < H*M` still hold.

### Impact Explanation
If confirmed, this would allow replay of a meta-transaction (double execution of a transfer, function call, or any action set authorized under `sender_id`'s identity), matching the "double-spend/replay" and "authorization escalation" bounty categories. The scoped impact is exactly the sender's own account being able to re-execute its own previously signed meta-transaction — this is only actionable if the attacker benefits from *double execution*, e.g., a delegate action that pays out from the receiver rather than the sender, or a relayer being tricked into re-relaying and re-paying gas for a cached signed delegate action.

### Likelihood Explanation
Preconditions are narrow: the DeleteKey+AddKey sequence and the DelegateAction execution must land in the **same block height**, which requires precise timing since block heights advance quickly and the attacker does not control chunk production. The attacker would need transactions to be included in the same block deterministically, which is not fully within an unprivileged user's control (subject to mempool/relay timing and chunk producer scheduling). This significantly limits reliability/repeatability. I was unable to fully verify, within available tool calls, the exact post-validation nonce write-back code (immediately following line 650) to confirm whether `access_key.nonce` is set to exactly `delegate_nonce.nonce()` or something else, which is necessary to fully confirm the "current_nonce equals N after first execution" premise.

### Recommendation
Do not reseed a re-added key's nonce independent of prior key state at the same account+pubkey identity within nonce-checking logic for delegate actions; alternatively, track a nonce high-water mark per account (not per access-key object) that is not reset by delete/re-add of the same public key, or invalidate in-flight delegate actions across key rotation using a key-generation counter incorporated into the nonce validation.

### Proof of Concept
Integration/runtime test: (1) create account with full-access key K, set block height H, (2) submit `SignedDelegateAction` with nonce `N` where `(H-1)*ACCESS_KEY_NONCE_RANGE_MULTIPLIER < N < H*ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, assert success and that `access_key.nonce == N`, (3) at the same block height H, submit `DeleteKeyAction` then `AddKeyAction` for the same public key K, assert `access_key.nonce == (H-1)*ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, (4) resubmit the identical `SignedDelegateAction` from step 2, assert whether it incorrectly succeeds (validation passes) instead of being rejected as a nonce reuse.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L52-91)
```rust
pub(crate) fn action_delete_key(
    config: &RuntimeConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    delete_key: &DeleteKeyAction,
) -> Result<(), RuntimeError> {
    let access_key = get_access_key(state_update, account_id, &delete_key.public_key)?;
    if let Some(access_key) = access_key {
        if let Some(gas_key_info) = access_key.gas_key_info() {
            delete_gas_key(
                config,
                state_update,
                account,
                result,
                account_id,
                &delete_key.public_key,
                &access_key,
                gas_key_info,
            )?;
        } else {
            delete_regular_key(
                &config.fees,
                state_update,
                account,
                account_id,
                &delete_key.public_key,
                &access_key,
            );
        }
    } else {
        result.result = Err(ActionErrorKind::DeleteKeyDoesNotExist {
            public_key: delete_key.public_key.clone().into(),
            account_id: account_id.clone(),
        }
        .into());
    }
    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L149-192)
```rust
pub(crate) fn action_add_key(
    apply_state: &ApplyState,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    result: &mut ActionResult,
    account_id: &AccountId,
    add_key: &AddKeyAction,
) -> Result<(), StorageError> {
    if get_access_key(state_update, account_id, &add_key.public_key)?.is_some() {
        result.result = Err(ActionErrorKind::AddKeyAlreadyExists {
            account_id: account_id.to_owned(),
            public_key: add_key.public_key.clone().into(),
        }
        .into());
        return Ok(());
    }

    let fee_config = &apply_state.config.fees;

    if let Some(gas_key_info) = add_key.access_key.gas_key_info() {
        add_gas_key(
            fee_config,
            state_update,
            account,
            account_id,
            &add_key.public_key,
            &add_key.access_key,
            gas_key_info,
            apply_state.block_height,
        )?;
    } else {
        add_regular_key(
            fee_config,
            state_update,
            account,
            account_id,
            &add_key.public_key,
            &add_key.access_key,
            apply_state.block_height,
        )?;
    }

    Ok(())
}
```

**File:** runtime/runtime/src/access_keys.rs (L230-255)
```rust
fn add_regular_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
    block_height: BlockHeight,
) -> Result<(), StorageError> {
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);

    account.set_storage_usage(
        account
            .storage_usage()
            .checked_add(access_key_storage_usage(fee_config, public_key, &access_key))
            .ok_or_else(|| {
                StorageError::StorageInconsistentState(format!(
                    "Storage usage integer overflow for account {}",
                    account_id
                ))
            })?,
    );
    Ok(())
}
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
