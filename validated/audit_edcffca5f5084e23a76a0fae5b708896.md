Based on my investigation, I found a genuine analog to the reported bug class in the access-key nonce reset behavior on `AddKey`/`DeleteKey`.

### Title
Access key nonce is unconditionally reset on re-creation instead of being carried forward, enabling replay of previously-signed transactions - (File: `runtime/runtime/src/access_keys.rs`)

### Summary
The external report's bug class is: a counter/state value that gates authorization (a vote tally) is not properly reset/preserved when the underlying value is reverted, letting an old, already-satisfied condition be reused to bypass a fresh authorization check. The closest reachable analog in nearcore is the access-key `nonce`, which is the counter that gates transaction replay. When an access key is deleted and a new key is added reusing the *same public key*, nearcore does not carry forward the old key's nonce state — it simply reseeds it from the current block height, contrary to the protocol's own documented invariant.

### Finding Description
`AccessKey::nonce` exists specifically "to avoid replaying old transactions again," and the struct's own doc comment states the required invariant explicitly: [1](#0-0) 

But `add_regular_key`, invoked from `action_add_key` whenever a regular `AddKey` action is applied, ignores any prior nonce state for that public key and unconditionally reseeds it from the current block height: [2](#0-1) 

`initial_nonce_value` derives the seed purely from `block_height`, with no knowledge of what nonce the previously-deleted key (same public key) had reached: [3](#0-2) 

`action_delete_key`/`delete_regular_key` simply removes the trie entry for the key — the nonce is discarded, not archived anywhere: [4](#0-3) 

This is a known, still-open gap acknowledged directly in the test suite: [5](#0-4) 

The nonce-validation logic in `verify_nonce` uses `NonceMode::Monotonic` by default, which only requires `tx_nonce > current_nonce` — gaps are explicitly allowed ("nonce 100 with monotonic mode: should succeed"): [6](#0-5) 

This means a signer can legitimately pre-sign a transaction with a nonce far ahead of the currently-used nonce (a documented and tested pattern) without ever broadcasting/executing it. Re-keying with the same public key is itself a documented, legitimate workflow — `FunctionCallPermission`'s doc explicitly instructs users to delete and recreate an access key (same key) to change its allowance: [7](#0-6) 

Putting these together: if an account holds a pre-signed, not-yet-executed transaction `T` with a high `nonce` (valid because Monotonic mode allows any future nonce below `block_height*1_000_000`), and the account owner (or an application/wallet acting on their behalf) later deletes and re-adds the *same public key* — e.g. to update a `FunctionCallPermission`'s allowance/receiver/methods as intentionally documented — the new key's nonce is reseeded from `(current_block_height - 1) * 1_000_000`. If this reseeded value is lower than `T`'s nonce (which happens whenever the re-creation occurs within the same/nearby block-height nonce-range window as when `T` was signed), `T` becomes valid and executable again, even though the user believed the old key state (and any transactions tied to it) had been invalidated by the key rotation. This exactly mirrors the reported bug class: a threshold/state counter (`VotesGCByVault` / access-key `nonce`) that is supposed to gate authorization is not carried over when the underlying value is replaced-then-reverted-to-equivalent, letting stale, already-authorized data bypass the intended fresh-authorization boundary.

### Impact Explanation
An old signed transaction — which the account owner reasonably assumes is permanently invalidated by rotating (deleting + re-adding) the access key — can be replayed after the rotation. Since `AddKey` with a reused public key is an explicitly documented pattern for changing `FunctionCallPermission` allowance/receiver/methods, an attacker (or malicious relayer/dApp holding a copy of a previously signed-but-unexecuted transaction from the user) can later submit it and have it accepted, executing an action the user did not intend to authorize under the new key configuration (e.g., spending against a freshly-topped-up allowance, or performing a `FunctionCall`/`Transfer`/`Stake` the user believed was retired). This is a replay/authorization-bypass primitive that can result in fund loss depending on what the stale transaction contains.

### Likelihood Explanation
Reaching this requires: (1) a pre-signed transaction with a nonce ahead of the key's current nonce that was never broadcast/executed (a realistic scenario for relayers, session-key wallets, or any application that pre-signs batches of transactions for later use — a pattern the codebase itself supports via `NonceMode::Monotonic` allowing gaps), and (2) the account owner performing a documented delete+re-add of the *same* public key. Both are ordinary, unprivileged, user-triggered operations reachable via standard `AddKey`/`DeleteKey` actions — no validator/operator/network-level capability is required. The precision needed (reseeded nonce landing below the stale transaction's nonce) is bounded by the `ACCESS_KEY_NONCE_RANGE_MULTIPLIER = 1_000_000` per-block window, which is a large, easily-targetable range for an attacker who controls or predicts the pre-signed nonce value.

### Recommendation
When adding a regular access key whose public key matches a key that previously existed on the account (or was recently deleted), the new key's `nonce` should be seeded to be at least as large as any nonce previously reached for that `(account_id, public_key)` pair, not merely derived from the current block height. Concretely, before deleting a key, persist (or otherwise account for) its last-used nonce so `add_regular_key` can take `max(initial_nonce_value(block_height), previous_nonce)`, restoring the invariant documented in `docs/DataStructures/AccessKey.md`.

### Proof of Concept
1. Alice has access key `K` (public key `pk`) on her account with `FunctionCall` permission and low allowance.
2. Alice signs transaction `T` (e.g., a `FunctionCall` spending near the current allowance) with `nonce = current_block_height * 1_000_000 - 1` (a valid future nonce under `NonceMode::Monotonic`, per `verify_nonce` in `runtime/runtime/src/verifier.rs:211-228`), but does not broadcast it — it is only held (e.g., cached by a relayer/dApp).
3. Alice submits `DeleteKey(pk)` followed by `AddKey(pk, new_access_key)` (same public key) to top up/replace the allowance, as documented in `docs/DataStructures/AccessKey.md:37-38`.
4. `add_regular_key` (`runtime/runtime/src/access_keys.rs:230-241`) reseeds `nonce = initial_nonce_value(block_height)` for the new key, which — because it depends only on the current block height and not on `T`'s nonce — can be lower than `T`'s nonce if the rotation happens within the same `1_000_000`-nonce block-height window.
5. The relayer/attacker who held `T` submits it; `verify_nonce` accepts it because `T.nonce > new_key.nonce`, executing `T` against Alice's account under the rotated key even though Alice believed the rotation invalidated it.

### Citations

**File:** docs/DataStructures/AccessKey.md (L6-12)
```markdown
```rust
pub struct AccessKey {
    /// The nonce for this access key.
    /// NOTE: In some cases the access key needs to be recreated. If the new access key reuses the
    /// same public key, the nonce of the new access key should be equal to the nonce of the old
    /// access key. It's required to avoid replaying old transactions again.
    pub nonce: Nonce,
```

**File:** docs/DataStructures/AccessKey.md (L27-49)
```markdown
## AccessKeyPermission::FunctionCall

Grants limited permission to make [FunctionCall](../RuntimeSpec/Actions.md#functioncallaction) to a specified `receiver_id` and methods of a particular contract with a limit of allowed balance to spend.

```rust
pub struct FunctionCallPermission {
    /// Allowance is a balance limit to use by this access key to pay for function call gas and
    /// transaction fees. When this access key is used, both account balance and the allowance is
    /// decreased by the same value.
    /// `None` means unlimited allowance.
    /// NOTE: To change or increase the allowance, the old access key needs to be deleted and a new
    /// access key should be created.
    pub allowance: Option<Balance>,

    /// The access key only allows transactions with the given receiver's account id.
    pub receiver_id: AccountId,

    /// A list of method names that can be used. The access key only allows transactions with the
    /// function call of one of the given method names.
    /// Empty list means any method name can be used.
    pub method_names: Vec<String>,
}
```
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

**File:** runtime/runtime/src/access_keys.rs (L230-241)
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
```

**File:** integration-tests/src/tests/standard_cases/mod.rs (L1169-1174)
```rust
        Err(err) => {
            // TODO(#6724): This is a wrong error, the transaction actually
            // succeeds. We get an error here when we retry the tx and the second
            // time around it fails. Normally, retries are handled by nonces, but we
            // forget the nonce when we delete a key!
            assert_eq!(
```

**File:** runtime/runtime/src/verifier.rs (L211-228)
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
```
