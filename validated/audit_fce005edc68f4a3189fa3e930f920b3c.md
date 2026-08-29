### Title
Gas key nonce-index replay via DeleteKey+AddKey reset in same block - ([File: runtime/runtime/src/access_keys.rs])

### Summary
`add_gas_key` unconditionally resets every gas-key nonce slot to `initial_nonce_value(block_height)` whenever a public key is re-added as a gas key, with no check against nonces that were already consumed for that key earlier in the same block. Because `initial_nonce_value(H) = (H-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` is always strictly below the per-block upper bound `H * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` used by `verify_nonce`, an attacker can push a nonce index near the upper bound, consume it with a real transaction, then self-issue a `DeleteKey`+`AddKey` on the same public key to drag the recorded nonce back down below the already-consumed value — reopening the acceptance window for that exact, already-executed transaction.

### Finding Description
`add_gas_key` sets every nonce slot to the same block-height-derived baseline regardless of prior state: [1](#0-0) 

`initial_nonce_value` is defined purely from the current block height: [2](#0-1) 

`action_add_key` only refuses to add a key if one already exists for that public key; it does not track history for a key that was just deleted in the same action list/block: [3](#0-2) 

Nonce admission for gas-key transactions is checked with `verify_nonce`, which enforces a monotonic increase against the currently stored nonce and a hard ceiling tied to block height: [4](#0-3) 

Exploit flow (attacker only needs signing rights over their own account, consistent with the threat model):
1. At block height `H`, attacker adds a `GasKeyFullAccess` key (or already owns one) and funds it via `TransferToGasKeyAction`.
2. Attacker signs and submits `tx1`, a gas-key transaction on `nonce_index=0` with `nonce` close to the ceiling `H * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`. It passes `verify_nonce` and is applied, consuming `gas_key_info.balance` (e.g. via `WithdrawFromGasKeyAction` or a value-moving action).
3. In the same block, attacker submits a second transaction containing `DeleteKey(pk)` followed by `AddKey(pk, GasKeyFullAccess)` for the same public key. `action_delete_key`/`delete_gas_key` burns any remaining declared gas-key balance and removes the key, then `action_add_key`/`add_gas_key` recreates it and resets nonce index 0 to `initial_nonce_value(H) = (H-1) * MULTIPLIER` — strictly lower than `tx1`'s nonce, which sat close to `H * MULTIPLIER`.
4. Attacker resubmits `tx1` (identical signed bytes) on `nonce_index=0`. `verify_nonce` now sees `current_nonce = (H-1)*MULTIPLIER < tx1.nonce < H*MULTIPLIER`, so both the monotonic check and the ceiling check pass, and the transaction is re-admitted and re-executed.

No existing check in `access_keys.rs` or `verifier.rs` prevents this: the only anti-replay mechanism for gas keys is the per-index nonce counter, and that counter is unconditionally clobbered on re-add with no floor derived from previously-observed nonces for that key. The `DeleteKey`+`AddKey` sequence is a fully attacker-controlled, in-protocol action pair requiring no privileged role.

### Impact Explanation
This breaks the anti-replay/value-conservation invariant for gas-key transactions: a transaction that already moved or withdrew value from `gas_key_info.balance` (or performed any other value-transferring action) can be re-admitted and re-executed against the same underlying funding, causing a double-spend/double-withdrawal from the account's gas-key-funded balance. This matches NEAR's "double-spend/replay" and "theft/loss of user funds" bounty category, scoped to the attacker's own gas-key funded balance and any counterparties who received funds from the replayed action (e.g., a second payout to a receiver from a single deposit).

### Likelihood Explanation
Preconditions are cheap and fully within an ordinary user's control: own an account, add/own a `GasKeyFullAccess` key, fund it, and be able to get three transactions (`tx1`, `DeleteKey+AddKey`, replayed `tx1`) included by the network within the same block height window so the nonce ceiling arithmetic lines up. No validator, operator, or node compromise is required. The main practical constraint is whether the resubmitted transaction (identical bytes/hash) is accepted for (re-)inclusion by mempool/relay logic, which is outside `access_keys.rs`/`verifier.rs`; the protocol-level state machine itself has no independent guard against this once the nonce floor is reset, and meta-transaction relaying (explicitly in scope for this attacker profile) provides an alternate path to reintroduce previously-signed transaction bytes without needing the original mempool to still hold them.

### Recommendation
When resetting gas-key nonces in `add_gas_key`, do not blindly use `initial_nonce_value(block_height)` for indices being reused after a delete-then-readd in the same or a later block; instead persist (or floor against) the maximum nonce previously recorded for that `(account_id, public_key, nonce_index)` triple — e.g., only raise nonces, never lower them, or make `initial_nonce_value` monotonic per-key by reading any existing/previously-removed nonce state before resetting.

### Proof of Concept
Runtime unit test in `runtime/runtime/src/access_keys.rs`-style harness:
1. Set block height `H`. Add a `GasKeyFullAccess` key with `num_nonces=1` and fund `gas_key_info.balance` via `action_transfer_to_gas_key`.
2. Call `verify_and_charge_gas_key_tx_ephemeral`/apply a gas-key transaction on `nonce_index=0` with `nonce = H*ACCESS_KEY_NONCE_RANGE_MULTIPLIER - 1` that withdraws balance via `action_withdraw_from_gas_key`; assert balance decreases and nonce store updates to that value.
3. In the same block height `H`, call `action_delete_key` then `action_add_key` for the same public key with `GasKeyFullAccess`; assert (via `get_gas_key_nonce`) that nonce index 0 is now `initial_nonce_value(H) = (H-1)*MULTIPLIER`, which is less than the nonce used in step 2.
4. Re-apply the identical transaction from step 2 through `verify_and_charge_gas_key_tx_ephemeral`; assert it returns `TxVerdict::Success` (not rejected), and that `action_withdraw_from_gas_key` executes again, driving `gas_key_info.balance` below zero-equivalent (checked_sub failing on the second true balance) or otherwise double-crediting the receiving account relative to the single funding deposit.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L149-164)
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
