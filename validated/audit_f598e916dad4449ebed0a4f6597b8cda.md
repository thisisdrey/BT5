### Title
Gas key nonce checkpoint reset via DeleteKey+AddKey allows replay of previously-consumed gas-key transactions - ([File: runtime/runtime/src/access_keys.rs])

### Summary
`add_gas_key` reseeds every nonce slot of a gas key to `initial_nonce_value(block_height)`, i.e. `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, with no floor comparison against the previous high-water mark for that pubkey. Because `validate_delegate_action_key` only requires `delegate_nonce.nonce() > current_nonce` read from the (now-reset) state, and the transaction's own nonce is embedded in already-signed transaction bytes, an account holder can delete and re-add their own gas key (same pubkey, same `num_nonces`) to lower the stored nonce checkpoint below a nonce it previously consumed, then resubmit the old signed transaction to execute it a second time.

### Finding Description
`add_gas_key` (runtime/runtime/src/access_keys.rs:194-228) unconditionally overwrites every nonce slot for a gas key: [1](#0-0) 
using `initial_nonce_value(block_height)`: [2](#0-1) 

This reseed happens on every `AddKey` for a gas key, including one that immediately follows a `DeleteKey` for the same pubkey within the same account (`action_delete_key` fully removes the gas key's access key and all nonce rows, and `action_add_key` rejects re-adding only if the key still exists, so `DeleteKey` + `AddKey` in the same transaction succeeds unconditionally).

`validate_delegate_action_key` (runtime/runtime/src/actions.rs:563-650) authorizes a gas-key-nonce transaction purely from freshly-read state: [3](#0-2) 
and enforces only an upper bound tied to the *current* block height, not to when the nonce was originally consumed: [4](#0-3) 

Exploit flow:
1. Attacker (owner of a `GasKeyFullAccess` key with `num_nonces > 1`, funded via `TransferToGasKeyAction`) signs and submits a gas-key transaction at `nonce_index = 0` with `nonce = N`, chosen near the maximum allowed value for block height `H0` (`N` close to `H0 * M - 1`, where `M = ACCESS_KEY_NONCE_RANGE_MULTIPLIER`). This executes, advancing the stored nonce for slot 0 to `N`.
2. In a later transaction (own account, own keys — no privilege needed), the attacker submits `DeleteKey(pubkey)` followed by `AddKey(same pubkey, GasKeyFullAccess, same num_nonces)` at height `H1 >= H0`. `add_gas_key` resets slot 0 back to `(H1 - 1) * M`, which is strictly lower than `N` whenever `N > (H1 - 1) * M` — trivially true if `H1 == H0` (since `N ≈ H0*M - 1 > H0*M - M = (H0-1)*M` for `M > 1`), and remains true for any `H1` close to `H0`.
3. The attacker resubmits the original signed transaction bytes from step 1 (nonce still `N`). `validate_delegate_action_key` reads the new, lower `current_nonce = (H1-1)*M`, finds `N > current_nonce`, and the upper-bound check `N < apply_state.block_height * M` still holds because block height only increases. The transaction is re-executed.

The existing checks (nonce monotonicity, upper-bound range check, `AddKeyAlreadyExists`) do not prevent this because none of them track a floor derived from the pubkey's historical maximum nonce across delete/re-add cycles — `add_gas_key` treats every `AddKey` as if the pubkey were brand new, silently rewinding the anti-replay checkpoint.

### Impact Explanation
This allows double execution of a gas-key-authorized action (e.g., a transfer, contract call, or any delegate action funded via the gas key's prepaid balance) using only actions available to an ordinary, unprivileged account controlling its own keys. This matches "double-spend/replay" and "theft or permanent freezing of user funds" categories: any counterparty relying on the gas-key transaction executing exactly once (e.g., a paid service, a one-time payment, a relayer-submitted meta-transaction) can be made to see it execute twice, and the gas-key balance can be debited twice for what should be a single charged action.

### Likelihood Explanation
Fully attacker-controlled and repeatable: requires only a funded account with a `GasKeyFullAccess` key with `num_nonces > 1`, standard `TransferToGasKeyAction` funding, and the ability to submit ordinary transactions (`DeleteKey`, `AddKey`) plus replay of a previously-broadcast signed transaction — all within the stated "unprivileged client" threat model. No validator, network, or timing race is needed; the only requirement is picking a nonce near the top of the currently allowed range for the initial transaction, which the signer freely controls.

### Recommendation
When re-adding a gas key (or any access key) with a pubkey that previously existed, do not reset nonce slots below any value previously observed for that pubkey. Track and persist a monotonically non-decreasing floor per pubkey (or per account) independent of `DeleteKey`, so `initial_nonce_value(block_height)` is only ever used to raise, never lower, a nonce checkpoint. Alternatively, forbid `AddKey` from reusing a public key that was deleted within a bounded number of recent blocks, or make `initial_nonce_value` derive from `max(previous max nonce ever used by this pubkey/account, (block_height-1)*M)`.

### Proof of Concept
Integration/test-loop test:
1. Create account `A` with a `GasKeyFullAccess` key `K` with `num_nonces = 2`; fund via `TransferToGasKeyAction` with balance `B`.
2. At height `H0`, submit a gas-key delegate/meta-transaction using `nonce_index = 0`, `nonce = H0*M - 1`, performing an action with fixed, observable cost `C` (e.g., transfer to account `V`). Assert `V`'s balance increases by the transfer amount once and gas-key balance decreases by `C`.
3. At height `H1` close to `H0`, submit `DeleteKey(K)` + `AddKey(K, GasKeyFullAccess(num_nonces=2))` in one transaction from `A`. Assert via `get_gas_key_nonce` that slot 0 is now `(H1-1)*M`, lower than the previously consumed nonce.
4. Resubmit the exact transaction bytes from step 2. Assert it is accepted (`validate_delegate_action_key` passes) and executes again, doubling `V`'s balance increase and debiting the gas key's balance a second time for cost `C`, violating one-time execution / value conservation.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
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

**File:** runtime/runtime/src/actions.rs (L619-639)
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
        }
    };

    if delegate_nonce.nonce() <= current_nonce {
        result.result = Err(ActionErrorKind::DelegateActionInvalidNonce {
            delegate_nonce: delegate_nonce.nonce(),
            ak_nonce: current_nonce,
        }
        .into());
        return Ok(());
    }
```

**File:** runtime/runtime/src/actions.rs (L641-650)
```rust
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
