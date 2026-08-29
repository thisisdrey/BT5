### Title
Same-height DeleteKey+AddKey nonce reseed enables replay of already-executed transactions/delegate actions signed by that key - (File: `runtime/runtime/src/access_keys.rs`)

### Summary
`initial_nonce_value` deterministically reseeds an access key's nonce to `(block_height-1)*ACCESS_KEY_NONCE_RANGE_MULTIPLIER` on every `AddKey`, purely as a function of the current block height, independent of any higher nonce the same key reached earlier in that same height. Because the nonce upper bound enforced by `verify_nonce`/`validate_delegate_action_key` is also `block_height*ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, any transaction or delegate action previously executed by that key within the current height's nonce window becomes valid again after a same-height `DeleteKey`+`AddKey` cycle, breaking the no-replay invariant.

### Finding Description
`initial_nonce_value` at `runtime/runtime/src/access_keys.rs:46-50` computes the reseed nonce solely from `block_height`: [1](#0-0) 
`add_regular_key` (`access_keys.rs:230-255`) unconditionally overwrites `access_key.nonce` with this value on every `AddKey`, regardless of what nonce the key previously reached: [2](#0-1) 

Nonce validation (`verifier.rs:211-237`) requires `tx_nonce > current_nonce` and `tx_nonce < block_height*ACCESS_KEY_NONCE_RANGE_MULTIPLIER`: [3](#0-2) 
The identical upper-bound/lower-bound pattern is used for delegate (meta-transaction) actions in `validate_delegate_action_key` (`runtime/runtime/src/actions.rs:632-650`): [4](#0-3) 

Exploit flow at fixed block height `H`:
1. Key's on-chain nonce is already inside the window `[(H-1)*M, H*M)` (e.g., from a prior tx at this same height, or from the key having been added earlier at height `H`).
2. Attacker submits `tx_A` (any action, e.g. transfer/function-call/delegate action) with `nonce = N1` where `(H-1)*M < N1 < H*M`. It executes normally; `access_key.nonce = N1`.
3. Attacker submits `DeleteKey(pk)` then `AddKey(pk, ...)` in the same chunk/height `H` (both are local receipts against the signer's own account, so they land in the same chunk as `tx_A`). `AddKey` resets `access_key.nonce` to `(H-1)*M` via `initial_nonce_value`, discarding the fact that `N1` was already consumed.
4. Attacker resubmits the byte-identical, previously-signed `tx_A` (same nonce `N1`, same actions, same block hash reference). It passes `verify_nonce` because `N1 > (H-1)*M` (the new stored nonce) and `N1 < H*M`. The action set in `tx_A` executes a second time.

Existing checks that would normally stop replay — signature validity, nonce monotonicity, block-hash freshness — do not catch this because the signature is still valid (this is a genuine byte-identical resubmission, not a forgery) and the nonce check compares only against the *current* stored nonce, which has been rolled back by the `AddKey` reseed rather than tracking the true historical maximum for that key.

### Impact Explanation
This breaks the "no-replay" invariant for any account-owner/relayer-controlled key: a previously executed transaction or delegate (meta-transaction) action can be re-executed at the same block height by cycling `DeleteKey`+`AddKey` on the same public key. If the replayed action is a state-changing call (e.g., claiming a reward, voting, or a transfer authorized via a delegate action held by a relayer), it can be executed twice from a single original authorization — matching the "double-spend/replay" bounty category (token inflation/loss or double-spend where a contract or counterparty relies on single-use nonce semantics for a signed authorization).

### Likelihood Explanation
Preconditions are fully within an unprivileged attacker's control: they need only their own account, its own key, and the ability to batch several self-targeting transactions (`DeleteKey`/`AddKey` are local receipts processed in the same chunk as their transaction) at one block height. No validator/node privileges are required. The attack is repeatable at will and costs only the gas for the extra `DeleteKey`+`AddKey`+replay transactions. The main constraint is that the replayed tx's `block_hash` must still be within `transaction_validity_period`, which is easily satisfied since the cycle happens within the same block height as the original execution.

### Recommendation
Do not reset an access key's nonce to a value that can be lower than a nonce the same public key has already consumed at the current (or any prior) height. Options: persist a per-`(account_id, public_key)` high-water-mark nonce across delete/recreate cycles (e.g., a tombstone record retaining the last-used nonce, reseeding to `max(last_used_nonce, (block_height-1)*M)`), or otherwise ensure `AddKey` cannot roll the effective nonce floor backward relative to any nonce already accepted for that exact public key.

### Proof of Concept
Unit/integration test plan (extending `runtime/runtime/src/access_keys.rs` tests or `integration-tests/src/tests/features/access_key_nonce_for_implicit_accounts.rs`):
1. Create account with a `FullAccess` key at fixed `ApplyState.block_height = H`.
2. Execute `tx_A`: a `Transfer` action signed with `nonce = (H-1)*M + 5`; assert it succeeds and `access_key.nonce == (H-1)*M + 5`.
3. Execute `action_delete_key` then `action_add_key` for the same public key, still at `block_height = H`; assert stored `access_key.nonce == (H-1)*M` (per `initial_nonce_value`).
4. Re-submit the identical `tx_A` bytes (same nonce, same block hash) through `validate_verify_and_charge_transaction`/`verify_and_charge_tx_ephemeral` at `block_height = H`; assert it is accepted (`TxVerdict::Success`) and the transfer action executes a second time — demonstrating replay of an already-executed transaction while `access_key.nonce` was pinned back to `(H-1)*M` independent of the prior `N1` it had reached.
5. Repeat steps 2-4 for `N` cycles to show the reseed value stays `(H-1)*M` regardless of `N`, and that each cycle re-enables replay of `tx_A`.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L239-241)
```rust
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);
```

**File:** runtime/runtime/src/verifier.rs (L211-237)
```rust
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
