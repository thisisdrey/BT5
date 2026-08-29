### Title
Gas-key delete+re-add resets all `num_nonces` slots below already-consumed nonce values, enabling transaction replay - ([File: runtime/runtime/src/access_keys.rs::add_gas_key])

### Summary
`add_gas_key` unconditionally reseeds **every** nonce index of a newly (re-)added gas key to `initial_nonce_value(block_height)`, without checking whether that public key previously existed and had already consumed higher nonce values on some index. Combined with `delete_gas_key` wiping the per-index nonce rows on deletion, an attacker who consumes a nonce close to the block's nonce ceiling on one index, then deletes and re-adds the same public key as a gas key within the same block, drops the on-chain nonce floor for that index back down, re-opening a window in which the already-executed transaction (or any smaller nonce in that window) passes `verify_nonce` again.

### Finding Description
`add_gas_key` (`runtime/runtime/src/access_keys.rs:194-227`) does: [1](#0-0) 
```
    // Set up nonces for gas key
    let num_nonces = gas_key_info.num_nonces;
    let nonce = initial_nonce_value(block_height);
    for i in 0..num_nonces {
        set_gas_key_nonce(state_update, account_id.clone(), public_key.clone(), i, nonce);
    }
```
`initial_nonce_value(block_height) = (block_height-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER (1_000_000)` [2](#0-1) , which is strictly less than the per-block nonce upper bound used by `verify_nonce`: `upper_bound = block_height * 1_000_000` [3](#0-2) .

`delete_gas_key` removes every nonce row for the key (`for i in 0..gas_key_info.num_nonces { remove_gas_key_nonce(...) }`) and removes the access key itself [4](#0-3) . `action_add_key` only rejects re-adding a key if the exact same public key currently exists (`AddKeyAlreadyExists`, checked before dispatch) [5](#0-4) ; it does not check history of a previously-deleted key with the same public key, and `add_gas_key` has no knowledge of, or defense against, nonce values previously consumed by that same public key.

Nonce validation is purely a comparison against the *currently stored* per-index nonce plus the same-block upper bound (`verify_nonce`, Monotonic mode requires `tx_nonce > current_nonce`) [6](#0-5) . There is no other replay-protection mechanism in this codebase for gas-key nonces (per the "Nonce monotonicity prevents replay" invariant documented for this exact component) [7](#0-6) .

Exploit flow:
1. Attacker's gas key on nonce index `i` has a stored nonce close to the ceiling — e.g., attacker crafts and submits a gas-key transaction with `tx_nonce = upper_bound - 1` on index `i`. It passes `verify_nonce` and executes, updating the on-chain nonce for index `i` to `upper_bound - 1`.
2. In the same block, the attacker (using their regular full-access key, a separate nonce space) submits `DeleteKey` for the gas-key public key, then `AddKey` re-adding the same public key as a gas key (`GasKeyFullAccess`/`GasKeyFunctionCall`).
3. `delete_gas_key` wipes all nonce rows; `add_gas_key` reseeds every index, including `i`, to `initial_nonce_value(block_height) = (block_height-1)*1_000_000`, far below the just-consumed `upper_bound - 1`.
4. The attacker rebroadcasts the exact original signed transaction from step 1 (still valid: same block-height/recent block hash, same signature). `verify_nonce` now compares `tx_nonce (upper_bound - 1)` against the reset `current_nonce ((block_height-1)*1_000_000)` — the check passes, and the transaction (and its associated action — e.g., a `Transfer`) executes a second time.

No existing check (signature, access-key permission, gas metering, storage staking, size limits) blocks this, because the only replay guard is the per-index nonce, and that guard is what gets reset.

### Impact Explanation
This is a double-spend/replay of a previously-charged gas-key transaction: any value-moving action (e.g. `Transfer`, `FunctionCall` with deposit) executed under the gas key on index `i` can be re-executed, causing double-crediting of the receiver and/or double-charging (or non-charging, depending on which side is replayed) of the gas-key balance/account. This falls under "double-spend/replay" in the stated impact categories.

### Likelihood Explanation
Preconditions are all attacker-controlled and require no privileged access: an ordinary account owner who has added a `GasKeyFullAccess`/`GasKeyFunctionCall` key to their own account can trigger the full sequence (gas-key tx, then `DeleteKey`+`AddKey` on the same public key) purely with their own signed transactions. The only non-trivial requirement is landing all transactions within the same block, which the attacker can influence by controlling their own nonces/timing and submitting the transactions back-to-back; this is a timing/ordering dependency rather than a privilege requirement, and is repeatable — the attacker can retry until the transactions land together.

### Recommendation
`add_gas_key` should not blindly reseed nonces to `initial_nonce_value(block_height)` when re-adding a public key that may have previously existed as a gas key. Either (a) persist a per-account monotonically increasing "high-water mark" nonce ceiling per public key (or per account) that survives key deletion and is used as the floor for any future reseed, or (b) forbid reusing a public key for a new gas key within some safety window/at all after deletion, or (c) seed nonces using the maximum of `initial_nonce_value(block_height)` and the previous stored nonce values captured at delete time (requires persisting them across deletion instead of removing the rows outright, e.g., a tombstone with the last nonce values).

### Proof of Concept
Runtime/unit test plan (extends existing `access_keys.rs` test helpers):
1. Use `setup_account`/`add_gas_key_to_account` at `TEST_GAS_KEY_BLOCK_HEIGHT` with `num_nonces = 1`.
2. Manually set the gas key's nonce index `0` via `set_gas_key_nonce` to `upper_bound - 1` (i.e., `TEST_GAS_KEY_BLOCK_HEIGHT * ACCESS_KEY_NONCE_RANGE_MULTIPLIER - 1`), simulating a just-consumed transaction.
3. Call `action_delete_key` for that public key; assert nonce row is gone (`get_gas_key_nonce` returns `None`).
4. Call `action_add_key` re-adding the same public key as a gas key at the same `block_height`; assert `get_gas_key_nonce(..., 0)` now equals `initial_nonce_value(block_height)`, which is `< upper_bound - 1`.
5. Assert that `verify_nonce(tx_nonce = upper_bound - 1, current_nonce = initial_nonce_value(block_height), Some(block_height), Monotonic)` returns `Ok(())` — i.e., a transaction carrying the previously-consumed nonce value is accepted again, proving the replay window exists.

### Citations

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

**File:** runtime/runtime/src/access_keys.rs (L157-164)
```rust
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

**File:** runtime/runtime/src/verifier.rs (L211-236)
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
```

**File:** protocol-model/spec/accounts-keys.md (L111-111)
```markdown
- **Nonce monotonicity prevents replay**: `verify_nonce` rejects stale/equal (Monotonic) or non-sequential (Strict) nonces (`verifier.rs:212`).
```
