### Title
Deleting and re-adding a `FullAccess` key resets its nonce below already-used values, enabling exact replay/double-execution of previously-signed transactions - (File: runtime/runtime/src/access_keys.rs)

### Summary
`add_regular_key` unconditionally overwrites a re-added access key's nonce with `initial_nonce_value(block_height) = (block_height-1)*ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, discarding the previous key's actual nonce state, which `delete_regular_key` erases from the trie entirely. Because typical valid nonces at height `H` already lie in `[(H-1)*MULTIPLIER, H*MULTIPLIER)` (enforced by `verify_nonce`'s `NonceTooLarge` check), an attacker's own already-executed, already-signed transaction can be resubmitted and pass the Monotonic nonce check again after a `DeleteKey`+`AddKey` round-trip on the same public key, causing it to execute a second time.

### Finding Description
`initial_nonce_value` (runtime/runtime/src/access_keys.rs:46-50) computes a nonce floor purely from `block_height`, with no reference to any nonce previously used by the same public key: [1](#0-0) 

`add_regular_key` applies this floor unconditionally whenever a public key is (re-)added, even though `docs/DataStructures/AccessKey.md` documents the intended invariant that "if the new access key reuses the same public key, the nonce of the new access key should be equal to the nonce of the old access key. It's required to avoid replaying old transactions again." The runtime does not implement this invariant — the `nonce` field on the incoming `AddKeyAction.access_key` is discarded and replaced by `initial_nonce_value(block_height)`: [2](#0-1) 

`delete_regular_key` removes the access key trie entry (including its nonce) completely with no memory retained: [3](#0-2) 

`verify_nonce` (runtime/runtime/src/verifier.rs:210-237) uses Monotonic mode (`tx_nonce > current_nonce`) for legacy transactions, and separately enforces `tx_nonce < block_height * MULTIPLIER` (`NonceTooLarge`): [4](#0-3) 

Exploit flow, all performed by the unprivileged owner of account `A` using their own `FullAccess` key `K` (and optionally a second self-controlled key to sign the delete/add step):
1. At chunk height `H`, submit `Transfer` action with `nonce = S+5`, signed by `K`, where `S+5` satisfies the required range `(H-1)*MULTIPLIER ≤ S+5 < H*MULTIPLIER` (this is the normal/expected nonce range for any fresh legitimate transaction at height `H`). This executes, `verify_and_charge_tx_ephemeral` persists `access_key.nonce = S+5`, and the transfer amount is deducted/credited.
2. Submit a transaction with actions `[DeleteKey(K), AddKey(K)]` (signed by `K` itself, or any other FullAccess key on the same account). `action_delete_key` removes `K`'s entire access-key trie entry (nonce `S+5` is lost); `action_add_key` → `add_regular_key` recreates `K` with `nonce = initial_nonce_value(H) = (H-1)*MULTIPLIER`.
3. Resubmit the ORIGINAL signed bytes of the step-1 `Transfer` (`nonce = S+5`). Since `S+5 ≥ (H-1)*MULTIPLIER` (this held by construction in step 1), `verify_nonce`'s Monotonic check `tx_nonce > current_nonce` (`S+5 > (H-1)*MULTIPLIER`) passes trivially, and the `NonceTooLarge` check at the (same or later) application height also passes. The transfer executes a second time.

No existing check stops this: signature verification only proves `K` signed the bytes (true, unchanged); nonce verification is reset to a value strictly lower than what the previously-consumed nonce needs to exceed; there is no chain-level transaction-hash-replay ledger (that mechanism was explicitly replaced by the height-based nonce floor per issue #3779, but that floor only protects against *arbitrary low* nonces such as `0`/`1`/`2`, not against nonces near the current height's own valid range, which is exactly what a legitimately-issued and already-executed transaction would use).

### Impact Explanation
This is a double-spend / double-execution primitive: replaying a `Transfer` (or any other action with balance-affecting side effects, e.g. `FunctionCall` with a deposit) reruns it in a second receipt, duplicating the deduction/credit and diverging from the "exactly-once execution" guarantee nonces are meant to provide. Concrete impact category: double-spend/replay leading to loss/duplication of user funds. Because this is entirely self-inflicted against the attacker's own account and its own recipient of choice, the most damaging variant is inflating a receiving account's balance twice for a single signed authorization (e.g., replaying a `Transfer` action to a contract that credits an internal ledger, or replaying a `FunctionCall` deposit to mint/credit something), i.e., token inflation / double-crediting at the receiver rather than a loss to a third party, since the sender loses funds twice from their own balance while the receiver is credited twice — this can be leveraged against any third-party contract that trusts "one transaction = one credit."

### Likelihood Explanation
Preconditions are minimal and entirely within an unprivileged user's control: own an account, hold a `FullAccess` key, and be able to issue `DeleteKey`+`AddKey` for that same public key (a completely normal, unrestricted action). No validator/node privilege, no leaked keys, no social engineering. Cost is just the gas/fees for one extra `DeleteKey`+`AddKey` transaction. The exploit is deterministic and repeatable (can be repeated indefinitely by re-deleting/re-adding the key and replaying any previously-signed transaction whose nonce still exceeds the new floor).

### Recommendation
When recreating an access key with the same public key (`add_regular_key`/`add_gas_key`), the new nonce must never be lower than any nonce previously associated with that public key on this account. Since the old nonce is erased on delete, either (a) do not delete the nonce state on `DeleteKey`, retaining a tombstone with the last known nonce so `AddKey` can seed `max(old_nonce, initial_nonce_value(block_height))`, or (b) disallow `AddKey` from ever reusing a public key that was deleted within the current transaction-validity window without carrying forward the previous nonce explicitly. This restores the documented invariant in `docs/DataStructures/AccessKey.md` that recreated keys must preserve nonce monotonicity for replay protection.

### Proof of Concept
Runtime apply-path integration test (test-loop-tests or `integration-tests`):
1. Create account `A` with initial balance, add `FullAccess` key `K` at height `H0`.
2. At height `H` (H0 < H), submit and apply `Transfer(deposit=D)` from `A` to `B`, signed by `K` with `nonce = S+5` where `S+5` is in `[(H-1)*MULTIPLIER, H*MULTIPLIER)`. Assert it succeeds; record `B`'s balance (`bal1`) and keep the raw signed transaction bytes.
3. In the same or a subsequent chunk still at height `H` (or a later height, before any further nonce advance on `K`), submit `[DeleteKey(K), AddKey(K)]` signed by `A`'s second `FullAccess` key. Assert success and that `view_access_key(K).nonce == (H-1)*MULTIPLIER`.
4. Resubmit the exact original signed `Transfer` bytes from step 2. Assert `ProcessTxResponse::ValidTx` (not `InvalidNonce`), and after chunk application assert `B`'s balance equals `bal1 + D` (i.e., credited twice total: `initial + 2D`), and `A`'s balance was debited `D` twice — demonstrating double execution of the same signed transaction.

### Citations

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
