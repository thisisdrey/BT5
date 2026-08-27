### Title
Deleting and re-adding an access key resets its nonce to a block-height-derived value instead of preserving the key's prior nonce, enabling replay of previously-executed transactions - (File: `runtime/runtime/src/access_keys.rs`)

### Summary
`AddKeyAction` always initializes a (re-)added access key's nonce from `initial_nonce_value(block_height)`, regardless of whether the same public key previously existed on the account and had already advanced its nonce to a higher value. This violates the documented invariant that recreating an access key with the same public key must preserve the previous nonce to prevent replaying old transactions, and it lets an account holder cause a previously-executed transaction to be replayed against their own account after a `DeleteKey` + `AddKey` cycle.

### Finding Description
The `AccessKey` documentation explicitly states the required invariant:

> "NOTE: In some cases the access key needs to be recreated. If the new access key reuses the same public key, the nonce of the new access key should be equal to the nonce of the old access key. It's required to avoid replaying old transactions again." [1](#0-0) 

However, `action_add_key` never inspects any prior nonce for the public key being added — it dispatches to `add_regular_key` (or `add_gas_key`) purely based on whether an access key with that public key currently exists, and `AddKeyAlreadyExists` only guards against adding a key while one is still present: [2](#0-1) 

`add_regular_key` unconditionally sets the new key's nonce from the current block height, with no consultation of any previously recorded nonce for that public key: [3](#0-2) 

```rust
fn add_regular_key(...) {
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    ...
}
```

`initial_nonce_value` is purely a function of the *current* block height: [4](#0-3) 

```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

Meanwhile `delete_regular_key` simply removes the access key trie entry, discarding its nonce entirely — there is no tombstone or "last known nonce" record kept for the public key: [5](#0-4) 

Nonce validation at transaction verification time (`verify_nonce`) only compares the submitted `tx_nonce` against the *current* on-chain `access_key.nonce` and an upper bound of `block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` — it has no memory of any nonce previously used by a key with the same public key before it was deleted and recreated: [6](#0-5) 

Because `ACCESS_KEY_NONCE_RANGE_MULTIPLIER` is `1_000_000` and the upper bound for a valid nonce at block height `h` is `h * 1_000_000`, a signer can legally use a transaction nonce anywhere up to just under `h * 1_000_000` at block height `h`. If that access key is subsequently deleted and an `AddKey` for the *same public key* is submitted (even within the very next block, or the same transaction batch of actions), the freshly created key's nonce is reset to `(h-1) * 1_000_000` (or based on whatever the current block height is at the time of re-add) — a value that can be far *below* the nonce that was already consumed by a prior transaction signed with the same key.

This directly matches the report's bug class: stale/prior state (`_listing` mapping / access-key nonce) is not properly invalidated or carried forward when a new entity (protected listing / recreated access key) supersedes it, so removing the new entity (unlocking the protected listing / deleting-then-recreating the key) resurrects access to the old, already-consumed state (stale listing / already-used nonce range), allowing re-execution of an operation that should be permanently consumed.

### Impact Explanation
Any previously-signed (and already broadcast/executed) transaction that used a nonce between the new `initial_nonce_value` and the old key's consumed nonce becomes valid again and can be resubmitted and re-executed by anyone holding a copy of that old signed transaction bytes (transactions are gossiped/visible on-chain, or the original owner/relayer may hold a copy). This is a genuine transaction replay: a `Transfer`, `FunctionCall`, or any other previously-executed action tied to that access key can be re-applied to the account, causing unintended double-spending/duplicate execution of funds transfers or contract calls signed under that key. This satisfies the "double-spend/replay" acceptance criterion for this analog scan.

### Likelihood Explanation
The attacker (or an observer who captured an old signed transaction) needs: (1) an access key whose nonce was pushed close to the current block-height-derived upper bound (achievable by the key's own legitimate owner, or via any account operation that consumes high nonces), (2) a `DeleteKey` followed by `AddKey` for the same public key (two ordinary, unprivileged actions, includable in the same transaction or consecutive transactions), and (3) possession of an old, already-executed signed transaction using a nonce that falls in the now-reopened gap. All three steps use only standard, unprivileged transaction/action APIs available to any account holder — no special privileges, validator behavior, or network manipulation required.

### Recommendation
When adding a key whose public key was previously used and deleted on the account, the new access key's nonce must be initialized to at least the maximum nonce ever previously associated with that public key (not merely derived from the current block height). This requires persisting a "last used nonce" per public key across deletion (e.g., a tombstone entry) and having `add_regular_key`/`add_gas_key` read and respect it, per the invariant already documented in `docs/DataStructures/AccessKey.md`.

### Proof of Concept
1. Account `A` has access key `K` (full access) with `nonce = 0` at block height `h0`.
2. `A` signs and submits a transaction (e.g., large token transfer) with `tx_nonce = h1 * 1_000_000 - 1` at some later block height `h1` (just under the `NonceTooLarge` upper bound, permitted by `verify_nonce`, see `runtime/runtime/src/verifier.rs:229-235`). The access key's stored nonce becomes `h1 * 1_000_000 - 1`.
3. At block height `h1` (or shortly after), `A` submits a transaction with two actions: `DeleteKey(K)` followed by `AddKey(K, ...)` for the same public key `K` (`action_delete_key` / `action_add_key`, `runtime/runtime/src/access_keys.rs:52`, `:149`). Because `AddKeyAlreadyExists` only checks presence in the trie (`access_keys.rs:157`), this succeeds since `K` was just deleted in the prior action of the same receipt.
4. The re-added key's nonce is now `initial_nonce_value(h1) = (h1-1) * 1_000_000` (`access_keys.rs:230-255`), which is smaller than the nonce `h1 * 1_000_000 - 1` already consumed in step 2.
5. The original signed transaction bytes from step 2 (`tx_nonce = h1 * 1_000_000 - 1`, signed by `K`) are resubmitted. `verify_nonce` sees `tx_nonce (h1*1_000_000 - 1) > current_nonce ((h1-1)*1_000_000)` and accepts it again (`runtime/runtime/src/verifier.rs:218-221`), causing the transfer/action to execute a second time.

### Citations

**File:** docs/DataStructures/AccessKey.md (L8-11)
```markdown
    /// The nonce for this access key.
    /// NOTE: In some cases the access key needs to be recreated. If the new access key reuses the
    /// same public key, the nonce of the new access key should be equal to the nonce of the old
    /// access key. It's required to avoid replaying old transactions again.
```

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L136-147)
```rust
fn delete_regular_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
) {
    let storage_usage = access_key_storage_usage(fee_config, public_key, access_key);
    remove_access_key(state_update, account_id.clone(), public_key.clone());
    account.set_storage_usage(account.storage_usage().saturating_sub(storage_usage));
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

**File:** runtime/runtime/src/verifier.rs (L210-237)
```rust
/// Verify that the transaction nonce is valid.
fn verify_nonce(
    tx_nonce: Nonce,
    current_nonce: Nonce,
    block_height: Option<BlockHeight>,
    nonce_mode: NonceMode,
) -> Result<(), InvalidTxError> {
    match nonce_mode {
        NonceMode::Monotonic => {
            if tx_nonce <= current_nonce {
                return Err(InvalidTxError::InvalidNonce { tx_nonce, ak_nonce: current_nonce });
            }
        }
        NonceMode::Strict => {
            if !current_nonce.checked_add(1).is_some_and(|expected| tx_nonce == expected) {
                return Err(InvalidTxError::InvalidNonce { tx_nonce, ak_nonce: current_nonce });
            }
        }
    }
    if let Some(height) = block_height {
        let upper_bound = height
            .saturating_mul(near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER);
        if tx_nonce >= upper_bound {
            return Err(InvalidTxError::NonceTooLarge { tx_nonce, upper_bound });
        }
    }
    Ok(())
}
```
