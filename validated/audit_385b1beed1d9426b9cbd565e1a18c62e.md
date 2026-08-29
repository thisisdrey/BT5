### Title
DelegateAction nonce replay after same-block key delete+re-add resets access-key nonce below previously consumed value - (File: runtime/runtime/src/access_keys.rs, runtime/runtime/src/actions.rs)

### Summary
`validate_delegate_action_key` in `runtime/runtime/src/actions.rs` authorizes a `DelegateAction` solely by checking `delegate_action.nonce` against the *currently stored* `access_key.nonce`, then persisting `access_key.nonce = delegate_action.nonce`. If the same public key is deleted and re-added within the same block, `add_regular_key` in `runtime/runtime/src/access_keys.rs` unconditionally resets the nonce to `initial_nonce_value(block_height) = (block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [1](#0-0) [2](#0-1) , ignoring the previously consumed nonce. Because a valid `DelegateAction` nonce must lie in `((block_height-1)*MULT, block_height*MULT)` to pass both the lower-bound and `upper_bound` checks, the reseeded value is always strictly less than any nonce that was already consumed in the same block, allowing the exact same `SignedDelegateAction` to be replayed and re-executed.

### Finding Description
`validate_delegate_action_key` reads the access key for the delegate's public key, rejects the action if `delegate_action.nonce <= access_key.nonce`, rejects it if `delegate_action.nonce >= apply_state.block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (the `upper_bound`), and otherwise updates and persists `access_key.nonce = delegate_action.nonce` as the sole replay guard (import confirms this logic lives in `actions.rs`) [3](#0-2) .

Separately, `action_add_key` dispatches to `add_regular_key` for non-gas-key permissions, which sets the freshly-added key's nonce to `initial_nonce_value(block_height)` regardless of any nonce that public key had before deletion [2](#0-1) . `initial_nonce_value` is computed purely from the current block height: `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [1](#0-0) .

Exploit flow:
1. Attacker owns account `S` with a FullAccess key `K`, and crafts/signs `SignedDelegateAction` with nonce `N` where `(block_height-1)*MULT < N < block_height*MULT` (a normal, valid nonce for the current block).
2. Attacker (as relayer, or via a colluding relayer) submits a meta-transaction wrapping this `SignedDelegateAction`; it executes once, `access_key(K).nonce` is set to `N`.
3. In the same block, attacker submits `DeleteKey(K)` + `AddKey(K, ...)` from account `S` (attacker owns `S`, no leaked keys needed). `add_regular_key` resets `access_key(K).nonce` to `(block_height-1)*MULT`, which is `< N` by construction.
4. Attacker resubmits the identical `SignedDelegateAction` (same signature bytes, same nonce `N`) as a new outer transaction. `validate_delegate_action_key` sees `N > access_key.nonce` (now the reseeded floor) and `N < upper_bound`, so it accepts and re-executes the delegate action's inner actions a second time.

The existing nonce/replay check only compares against the persisted `access_key.nonce`, and nothing ties that check to a per-nonce "already used" set or to the pre-delete nonce state; the reseed on re-add is height-derived only, not history-aware.

### Impact Explanation
This breaks the meta-transaction invariant (NEP-366) that a `SignedDelegateAction` executes exactly once, enabling double-execution of the delegated actions. If the delegated action is a `Transfer`/`FunctionCall` with a deposit directed at a third party (e.g., a faucet, one-time-claim contract, DAO payout, or a counterparty who accepted a single signed payment authorization), the second execution causes real, uncompensated fund movement — a double-spend/replay category impact.

### Likelihood Explanation
The attacker needs only: (a) a funded account with a FullAccess key, (b) the ability to submit `DeleteKey`+`AddKey` for their own key, and (c) the ability to relay (or collude with a relayer to resubmit) the identical `SignedDelegateAction` — all ordinary, unprivileged capabilities. No leaked keys, validator/node access, or social engineering of a third party is required since the attacker controls the signer account performing the delete/re-add. The nonce window constraint (`N` must fall between `(block_height-1)*MULT` and `block_height*MULT`) is trivially satisfiable since that is exactly the valid range for any current-block delegate nonce. The attack is repeatable once per block per key (bounded by needing a fresh delete+re-add per replay), and costs only the gas for the extra `DeleteKey`/`AddKey`/relayed actions.

### Recommendation
Do not reset an access key's nonce to a purely height-derived floor on re-add when the key is being re-created with the same public key; instead, either (a) track nonce state independent of key existence (e.g., persist a per-account-and-pubkey high-water-mark nonce that survives `DeleteKey`), or (b) require `AddKeyAction`'s effective nonce floor to be `max(initial_nonce_value(block_height), previous_max_nonce_seen_for_this_pubkey)`, or (c) bind delegate-action replay protection to a monotonic, key-independent counter (e.g., account-level nonce or explicit executed-hash set) rather than the mutable `AccessKey.nonce` field.

### Proof of Concept
Test-loop integration test in `test-loop-tests`:
1. Create account `S` with FullAccess key `K1`.
2. Build `SignedDelegateAction` with `nonce = N` (chosen within `((h-1)*MULT, h*MULT)` for the block height `h` at which it will execute) whose actions include a `Transfer` to account `R`.
3. Submit the meta-transaction; assert it succeeds and `R`'s balance increased by the transfer amount; assert stored `access_key(K1).nonce == N`.
4. In the same block, submit `DeleteKey(K1)` then `AddKey(K1, FullAccess)` from `S`.
5. Assert stored `access_key(K1).nonce == (h-1)*MULT < N`.
6. Resubmit the identical `SignedDelegateAction` (same bytes/signature, nonce `N`) as a new meta-transaction in the same block.
7. Expected (buggy) result: action succeeds and `R`'s balance increases a second time — demonstrating double-execution. Expected (fixed) result: action fails with `ActionErrorKind::DelegateActionInvalidNonce`.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
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

**File:** runtime/runtime/src/actions.rs (L1-1)
```rust
use crate::access_keys::initial_nonce_value;
```
