### Title
Access-key nonce rollback via same-block DeleteKey+AddKey enables transaction replay - ([File: runtime/runtime/src/access_keys.rs])

### Finding Description
`initial_nonce_value` at [1](#0-0)  computes the nonce baseline for a (re)created access key purely as `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, with no reference to any nonce previously used by that public key. `add_regular_key` unconditionally overwrites `access_key.nonce` with this value when a key is added, [2](#0-1) , and `action_delete_key`/`action_add_key` allow a single receipt to delete and immediately re-add the same public key, [3](#0-2) [4](#0-3) .

`verify_nonce` in the verifier enforces two independent checks: an ordering check against `current_nonce` (Monotonic: `tx_nonce > current_nonce`; Strict: exact increment for gas-key lanes), and a per-block-height admission bound `tx_nonce < block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, [5](#0-4) . The anti-hash-collision scheme (issue #3779, referenced in the comment at access_keys.rs:47-48) implicitly assumes that any nonce X previously admitted for a key must be `< H0 * MULTIPLIER` for the block height `H0` at which it was admitted, and that re-adding the key at a strictly later height `H1 > H0` produces a baseline `(H1-1)*MULTIPLIER >= H0*MULTIPLIER > X`, safely exceeding X.

That assumption fails when the delete+re-add happens at the **same** block height `H` as the original admission. If `tx1` (nonce `X`) is admitted with `(H-1)*MULTIPLIER < X < H*MULTIPLIER`, and a subsequent receipt at the same block height `H` performs `[DeleteKey(pk), AddKey(pk, FullAccess)]`, the new access key's nonce is reset to `(H-1)*MULTIPLIER`, which is strictly **below** `X`. The account's on-chain nonce for `pk` has effectively moved backward. Resubmitting the original signed `tx1` bytes to the RPC now passes `verify_nonce` under Monotonic mode (`X > (H-1)*MULTIPLIER`) and the same-block-height upper-bound check (`X < H*MULTIPLIER`), because block height has not advanced past `H`. Nothing else rejects an identical previously-executed signed transaction: signature verification succeeds (same keypair material re-added with `FullAccess`), and the runtime does not track a per-account "highest nonce ever seen for this pubkey" independent of the currently stored `AccessKey.nonce`.

### Impact Explanation
This is a determinism/replay violation: a signed transaction (e.g. a `Transfer` or `DelegateAction`) that already produced one receipt/execution outcome can be re-executed a second time after the signer deletes and re-adds their own key within the same block height, causing double payment/double withdrawal against any counterparty who trusted the first execution. This matches the "double-spend/replay" bounty category and constitutes real, permanent loss of funds for a counterparty (or double-mint/double-transfer effect for the attacker's own benefit).

### Likelihood Explanation
Preconditions are fully within an unprivileged attacker's control: the attacker owns the account and its `FullAccess` key, chooses `tx1`'s nonce to land near the current admission upper bound (trivial, since the attacker crafts and signs their own transactions), and submits a self-targeted `[DeleteKey, AddKey]` transaction as their second action — no special permissions, contract deployment, or third-party cooperation required. The only timing requirement is that both transactions land in the same block height, which is achievable for same-shard/self-account receipts processed within one chunk application. Cost is limited to two ordinary transaction fees. The attack is repeatable per key rotation, though it requires careful nonce/timing selection, which is realistic for a resourceful attacker probing RPC nonce admission windows.

### Recommendation
When re-adding a public key via `AddKeyAction` (`action_add_key`/`add_regular_key`/`add_gas_key`), the new nonce baseline should never be lower than any nonce previously observed for that same public key on that account. Concretely, either (a) track and persist a per-account "max nonce ever used" independent of `AccessKey.nonce` and use `max(initial_nonce_value(block_height), previous_max_nonce_for_pk)` as the reset value, or (b) disallow `DeleteKey` immediately followed by `AddKey` of the same public key within the same block height without forcing the new nonce baseline to advance past the current block's nonce range ceiling (`block_height * MULTIPLIER`) rather than its floor (`(block_height-1) * MULTIPLIER`).

### Proof of Concept
Runtime integration test (in `runtime/runtime/src/tests/apply.rs` or `integration-tests`):
1. Fund account `alice` with `FullAccess` key `pk`.
2. Submit `tx1 = Transfer(bob, amount)` signed by `pk` with nonce `X` chosen such that `(H-1)*MULTIPLIER < X < H*MULTIPLIER` for the block height `H` at which it will be applied; execute and assert `bob`'s balance increased by `amount` and the access key's stored nonce equals `X`.
3. In the same block height `H`, apply a receipt on `alice` with actions `[DeleteKey(pk), AddKey(pk, FullAccess)]`; assert the resulting `AccessKey.nonce` for `pk` equals `(H-1)*MULTIPLIER` (< `X`).
4. Resubmit the exact signed bytes of `tx1` (same nonce `X`, same signature) for validation/application at block height `H`; assert `verify_nonce`/`verify_and_charge_tx_ephemeral` accepts it (no `InvalidNonce`/`NonceTooLarge` error) and that it produces a second successful outcome, transferring `amount` to `bob` a second time — proving `bob`'s balance increased by `2 * amount` from a single originally-signed transaction.

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

**File:** runtime/runtime/src/access_keys.rs (L238-241)
```rust
) -> Result<(), StorageError> {
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);
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
