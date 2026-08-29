Confirmed: the trie key for a gas key nonce row is deterministic purely as a function of `(account_id, public_key, nonce_index)` via `TrieKey::gas_key_nonce` [1](#0-0)  and `remove_gas_key_nonce`/`get_gas_key_nonce` use the same deterministic key [2](#0-1) , with no generation/epoch counter tying a nonce row to a specific incarnation of the key. Reseeding on (re)creation is `initial_nonce_value(block_height) = (block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, a pure function of the current block height only [3](#0-2) [4](#0-3) . The nonce upper bound enforced at verification time is `tx_nonce < block_height * MULTIPLIER` [5](#0-4) , so a consumed nonce at height `h` can be as large as just under `h*MULTIPLIER`, while a delete+recreate that lands in the *same* block height `h` reseeds to `(h-1)*MULTIPLIER`, which is strictly lower. Only when recreation happens at a height `h' > h` does the reseed value dominate every previously-consumed nonce (`(h'-1)*MULTIPLIER >= h*MULTIPLIER`), which is exactly the protection the `initial_nonce_value` comment (referencing near/nearcore#3779) relies on.

### Title
Gas key nonce replay via same-block delete+recreate reseed collision - (File: runtime/runtime/src/access_keys.rs, runtime/runtime/src/verifier.rs, core/store/src/utils/mod.rs)

### Summary
`add_gas_key` reseeds every `nonce_index` row to `initial_nonce_value(block_height)`, a value that depends only on the current block height, not on the highest nonce ever consumed for that `(account_id, public_key, nonce_index)` trie row. If a gas key is deleted and a new key (possibly with different `num_nonces`) is added under the same public key within the same block height as a transaction that already consumed a nonce close to that height's upper bound, the reseeded value can be lower than the already-consumed nonce, allowing the original signed transaction to be replayed and re-applied.

### Finding Description
`initial_nonce_value` is `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [3](#0-2) , used unconditionally by `add_gas_key` to seed every nonce index of a newly-added gas key [4](#0-3) . The verifier's nonce upper bound is `tx_nonce < block_height * MULTIPLIER` [5](#0-4) , meaning a valid gas-key transaction nonce at block height `h` can be up to `h*MULTIPLIER - 1`.

The gas key's per-index nonce is stored under `TrieKey::gas_key_nonce(account_id, public_key, nonce_index)` [1](#0-0) , a deterministic key with no per-incarnation/generation tag. `delete_gas_key` fully removes these rows via `remove_gas_key_nonce` [6](#0-5) , and `action_add_key` only blocks re-adding a public key while an access key with that key still exists (`AddKeyAlreadyExists`) [7](#0-6) , so a fresh `AddKeyAction` for the same public key (even with a different `num_nonces`) is accepted right after a `DeleteKeyAction`.

Attack sequence, all initiated by the ordinary account owner (no privileged role needed):
1. At block height `h`, the owner signs and submits a gas-key transaction using `nonce_index = 0` with `tx_nonce = N`, where `N` is close to the per-height cap (`N < h*MULTIPLIER`), against a gas key with `num_nonces = 1`. This is applied, updating the stored nonce for index 0 to `N`.
2. Still within block height `h`, the owner submits `DeleteKeyAction` for that public key (using their regular full-access key with its own, unrelated nonce sequence) — this calls `delete_gas_key`, removing the access key and all nonce rows.
3. Still within block height `h`, the owner submits `AddKeyAction` re-adding the *same* public key as a gas key with `num_nonces = 3`. `add_gas_key` reseeds nonce index 0 (and 1, 2) to `initial_nonce_value(h) = (h-1)*MULTIPLIER`, which is `< N`.
4. The owner (or an unrelated relayer, if the original tx was a `DelegateAction`) rebroadcasts the *exact same signed transaction* from step 1 (same `tx_nonce = N`, `nonce_index = 0`). `verify_and_charge_gas_key_tx_ephemeral` reads `current_nonce` from the freshly reseeded row (`(h-1)*MULTIPLIER`) and `verify_nonce` accepts `N` again because `N > (h-1)*MULTIPLIER` [8](#0-7) , causing the transaction's effects (deposit transfer, function call, delegate action) to be executed twice.

This works only when steps 1–3 land in the same block height `h` (or a lower one, which is not achievable chronologically), because for any later height `h' > h`, `(h'-1)*MULTIPLIER >= h*MULTIPLIER > N`, so the reseed always dominates — this is precisely the protection the `initial_nonce_value` comment references (near/nearcore#3779). Nothing in the protocol enforces cross-key ordering constraints preventing a chunk from containing, in order, a gas-key transaction, a `DeleteKey` transaction, and an `AddKey` transaction for the same account within one block, since they use unrelated key nonce sequences.

### Impact Explanation
This is a double-spend/replay vulnerability (NEAR bounty category: double-spend/replay of a transaction). A replayed gas-key transaction can re-execute a `Transfer`, `FunctionCall`, or `DelegateAction` a second time, causing double-withdrawal from the gas key's balance/account balance or duplicate execution of a state-changing contract call, i.e., theft/duplication of funds or double-application of an action that should only happen once.

### Likelihood Explanation
The attacker must fully control timing so that the consuming transaction, the `DeleteKeyAction`, and the recreating `AddKeyAction` land in the same block/chunk in the right relative order, and the originally-consumed nonce for the target index must be close enough to the per-height cap that it exceeds `(h-1)*MULTIPLIER`. This is plausible for an attacker who fully controls their own account's key material and can simply retry the 3-step sequence across many blocks until favorable inclusion occurs — no validator or node privilege is required, only ordinary signed-transaction submission to a public RPC endpoint. Practical exploitation is timing-dependent rather than always deterministic, but is repeatable with unbounded retries at negligible cost (gas fees only).

### Recommendation
Make the gas-key nonce seed monotonic with respect to every value ever written for that `(account_id, public_key, nonce_index)` row, not just a function of block height — e.g., seed with `max(initial_nonce_value(block_height), previous_max_nonce_for_index)`, or retain a tombstone/generation marker per public key that prevents the same public key from being reused for a gas key within the same block height as its deletion, or simply reject `AddKeyAction` reusing a public key that was deleted earlier within the same block.

### Proof of Concept
Unit test in `runtime/runtime/src/access_keys.rs` or `runtime/runtime/src/verifier.rs` test modules:
1. At `block_height = h`, call `action_add_key` to create a gas key with `num_nonces = 1`; call `set_gas_key_nonce` (or apply a real gas-key tx) to raise nonce index 0 to `N = h*MULTIPLIER - 1`.
2. At the same `block_height = h`, call `action_delete_key` for that public key, then call `action_add_key` again for the *same* public key with `num_nonces = 3`.
3. Assert `get_gas_key_nonce(state_update, account_id, public_key, 0)` returns a value `< N` (demonstrating the seed no longer dominates the previously-consumed nonce).
4. Construct a `Transaction`/`TransactionCost` reusing `tx_nonce = N`, `nonce_index = 0`, and call `verify_and_charge_gas_key_tx_ephemeral`; assert it returns `TxVerdict::Success` instead of an `InvalidNonce` error, proving the previously-executed transaction nonce is accepted again.

### Citations

**File:** core/store/src/utils/mod.rs (L402-410)
```rust
pub fn set_gas_key_nonce_by_handle(
    state_update: &mut TrieUpdate,
    account_id: AccountId,
    key_handle: PublicKeyHandle,
    index: NonceIndex,
    nonce: Nonce,
) {
    set(state_update, TrieKey::gas_key_nonce(account_id, key_handle, index), &nonce);
}
```

**File:** core/store/src/utils/mod.rs (L420-455)
```rust
pub fn remove_gas_key_nonce(
    state_update: &mut TrieUpdate,
    account_id: AccountId,
    public_key: PublicKey,
    nonce_index: NonceIndex,
) {
    state_update.remove(TrieKey::gas_key_nonce(account_id, public_key, nonce_index));
}

pub fn get_access_key(
    trie: &dyn TrieAccess,
    account_id: &AccountId,
    public_key: &PublicKey,
) -> Result<Option<AccessKey>, StorageError> {
    get_access_key_by_handle(trie, account_id, &public_key.into())
}

/// Variant of [`get_access_key`] used by the trie-iteration paths
/// (`compute_gas_key_balance_sum`, `remove_account`, view RPC) that
/// already hold a `PublicKeyHandle` produced by the parse function.
pub fn get_access_key_by_handle(
    trie: &dyn TrieAccess,
    account_id: &AccountId,
    key_handle: &PublicKeyHandle,
) -> Result<Option<AccessKey>, StorageError> {
    get(trie, &TrieKey::access_key(account_id.clone(), key_handle.clone()))
}

pub fn get_gas_key_nonce(
    trie: &dyn TrieAccess,
    account_id: &AccountId,
    public_key: &PublicKey,
    index: NonceIndex,
) -> Result<Option<Nonce>, StorageError> {
    get(trie, &TrieKey::gas_key_nonce(account_id.clone(), public_key, index))
}
```

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L114-117)
```rust
    let num_nonces = gas_key_info.num_nonces as usize;
    for i in 0..gas_key_info.num_nonces {
        remove_gas_key_nonce(state_update, account_id.clone(), public_key.clone(), i);
    }
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

**File:** runtime/runtime/src/verifier.rs (L229-234)
```rust
    if let Some(height) = block_height {
        let upper_bound = height
            .saturating_mul(near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER);
        if tx_nonce >= upper_bound {
            return Err(InvalidTxError::NonceTooLarge { tx_nonce, upper_bound });
        }
```

**File:** runtime/runtime/src/verifier.rs (L415-419)
```rust
    let tx_nonce = tx.nonce().nonce();
    let effective_nonce = std::cmp::max(current_nonce, pending.max_nonce);
    if let Err(e) = verify_nonce(tx_nonce, effective_nonce, block_height, tx.nonce_mode()) {
        return TxVerdict::Failed(e);
    }
```
