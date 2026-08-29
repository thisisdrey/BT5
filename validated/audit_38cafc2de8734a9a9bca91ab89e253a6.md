### Title
Gas-key nonce reseed by `add_gas_key` uses only `block_height`, allowing an already-consumed nonce to be replayed after a same-block delete+recreate of the key - ([File: runtime/runtime/src/access_keys.rs])

### Finding Description
`initial_nonce_value(block_height)` returns `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` — a value that depends **only** on the current block height, not on any prior nonce state the key (or a same-pubkey predecessor) may have had. [1](#0-0) 

`add_gas_key` writes this exact value into every nonce slot (`set_gas_key_nonce`) whenever a gas key is (re)created, unconditionally: [2](#0-1) 

`delete_gas_key` removes all nonce rows and the access key entirely (no memory of previously issued nonces is retained anywhere in the trie): [3](#0-2) 

Because `initial_nonce_value` is a pure function of `block_height`, if within a single block/chunk apply the sequence `AddKey(pubkey, gas key)` → `GasKeyNonce tx (nonce = seed+1)` → `DeleteKey(pubkey)` → `AddKey(pubkey, gas key)` all execute (block_height unchanged throughout), the second `AddKey` reseeds the nonce slot back down to the *same* seed value that was current before the `GasKeyNonce` tx advanced it. `verify_nonce`'s Monotonic rule (`tx_nonce > current_nonce`) then accepts a resubmission/duplicate of a nonce value that was already consumed earlier in that same block, since the stored nonce has been rolled back below it. Nothing in `action_delete_key`/`add_gas_key` checks or preserves the previously-issued nonce watermark across a delete+recreate cycle within the same block.

### Impact Explanation
This breaks the documented invariant "nonce monotonicity prevents replay" (`verify_nonce`, `runtime/runtime/src/verifier.rs:212`) specifically for gas keys recreated within the same block as their prior use. A transaction whose authorization (nonce_index, nonce) was believed to be single-use can be re-executed, re-debiting the account's balance/gas-key balance and re-crediting a receiver — a double-spend/replay of a previously-authorized payment. This matches the "double-spend/replay" bounty category.

### Likelihood Explanation
Exploitation requires the entire chain (original `AddKey`, a `GasKeyNonce` tx consuming a nonce slot, `DeleteKey`, and `AddKey` with the identical public key) to land in the *same* block/chunk, which is a narrow, hard-to-guarantee timing window for an attacker to engineer against an arbitrary chunk producer's scheduling, and the account owner is generally the only party who can trigger it (since `DeleteKey`/`AddKey` require their own signing authority over the account). The scoped, practically exploitable effect is limited to the key owner replaying their own prior authorization against a fixed receiver within that same block — not an arbitrary cross-account fund theft. It is a real robustness gap in nonce monotonicity but has a narrow attack window.

### Recommendation
When recreating a gas key (or regular key) via `add_gas_key`/`add_regular_key`, do not seed the nonce purely from `block_height`. Instead, seed to `max(initial_nonce_value(block_height), <highest nonce ever recorded for this account+pubkey+nonce_index>)`, or retain a tombstone/watermark for deleted keys' highest-used nonce so a same-pubkey recreation can never reseed below a value already consumed.

### Proof of Concept
Add a unit test in `runtime/runtime/src/access_keys.rs` that, using a single fixed `ApplyState::block_height`:
1. Calls `action_add_key` to create a gas key with `num_nonces=1`.
2. Directly bumps the gas key nonce slot via `set_gas_key_nonce` (or by invoking the verifier's gas-key charging path) to `initial_nonce_value(block_height) + 1`, simulating a consumed `GasKeyNonce` transaction.
3. Calls `action_delete_key` then `action_add_key` again with the *same* `public_key` and the *same* `block_height`.
4. Asserts `get_gas_key_nonce` for that slot equals `initial_nonce_value(block_height)` again (proving the reseed collapsed back to the pre-consumption value).
5. Calls `verify_and_charge_gas_key_tx_ephemeral` (or `verify_nonce`) with `tx_nonce = initial_nonce_value(block_height) + 1` against the freshly reseeded `current_nonce`, and assert it is (incorrectly) accepted instead of returning `InvalidNonce`.

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

**File:** runtime/runtime/src/access_keys.rs (L209-214)
```rust
    // Set up nonces for gas key
    let num_nonces = gas_key_info.num_nonces;
    let nonce = initial_nonce_value(block_height);
    for i in 0..num_nonces {
        set_gas_key_nonce(state_update, account_id.clone(), public_key.clone(), i, nonce);
    }
```
