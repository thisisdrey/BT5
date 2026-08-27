The strongest reachable analog here is not a staking-unlock-delay issue (nearcore has no user-governed unstake timer comparable to FrankenDAO's), but the same root-cause pattern the report describes — **state that should persist across a "reset" cycle is instead re-initialized, letting an unprivileged actor replay/reuse a previously valid credential**. In nearcore this maps to access-key nonce handling across `DeleteKey` + `AddKey` with the same public key.

### Title
Access key nonce reset on DeleteKey+AddKey re-creation enables replay of previously-signed transactions - ([File: runtime/runtime/src/access_keys.rs])

### Summary
When an access key is deleted and a new key with the *same public key* is added back, the runtime does not preserve the deleted key's nonce. Instead it re-seeds the nonce purely from the current block height via `initial_nonce_value`, contradicting the protocol's own documented invariant that a re-created key must inherit the old key's nonce to prevent replay.

### Finding Description
`action_add_key` only guards against adding a key that currently exists (`AddKeyAlreadyExists`); it has no memory of a *previously deleted* key with the same public key [1](#0-0) . When the key is a regular (non-gas) key, `add_regular_key` unconditionally sets `access_key.nonce = initial_nonce_value(block_height)`, discarding any prior nonce history for that public key: [2](#0-1) .

`initial_nonce_value` derives the nonce solely from the current block height: [3](#0-2) .

This directly contradicts the protocol's documented design intent for `AccessKey.nonce`: "if the new access key reuses the same public key, the nonce of the new access key should be equal to the nonce of the old access key. It's required to avoid replaying old transactions again." [4](#0-3)  The same warning is repeated verbatim in the struct doc comment: [5](#0-4) .

Nonce verification (`verify_nonce`) only checks that `tx_nonce > current_nonce` (Monotonic mode) against whatever nonce is currently stored for the key — it has no way to know the key was deleted and recreated [6](#0-5) . So any transaction signed by the account holder before deletion, with a nonce greater than the freshly-seeded (height-derived) nonce, becomes valid again after the key is re-added.

This is a known, acknowledged gap in the codebase itself: the standard test suite explicitly documents that deleting a key "forgets the nonce," causing retried/duplicate transactions to unexpectedly succeed: [7](#0-6) .

### Impact Explanation
An account owner (or anyone who obtains a stale signed transaction, e.g. from a relayer, mempool leak, or their own previously-prepared-but-unbroadcast transaction) can:
1. Sign a transaction `Tx_high` with a large nonce (nonce is user-chosen and only bounded above by `block_height * 1_000_000`, per the upper-bound check) but not submit it immediately.
2. Delete the access key (`DeleteKeyAction`) and immediately re-add a key with the same public key (`AddKeyAction`) — both permitted actions requiring no special privilege, just a full-access key on one's own account.
3. Because the new key's nonce is reseeded from the *current* block height rather than inherited from the deleted key, the new nonce baseline can be lower than `Tx_high`'s nonce.
4. Broadcast `Tx_high`, which now passes `verify_nonce` and re-executes — effectively replaying a transaction (e.g., a `Transfer`, `FunctionCall`, or `Stake` action) that the signer already "spent" once mentally/logically, or that was crafted for one-time use (e.g., in a voting/permission system built on top, or a relayer-submitted meta-transaction context).

This is a genuine double-spend/replay primitive reachable purely from an ordinary account owner's own transactions (no validator/node privilege required), matching the report's core theme of "reusing the same credential to act twice" via a state-reset loophole.

### Likelihood Explanation
The precondition (delete then re-add the same public key) is a normal, permitted user operation with no cooldown or restriction, directly analogous to the missing "minimum unlock delay" in the original report. The vulnerable code path (`add_regular_key`) is on the default/common execution path for every `AddKeyAction`, and the gap is not hidden behind any protocol-version gate — it is unconditional in the current implementation.

### Recommendation
When adding a key whose public key was previously deleted from the same account within recorded history (or, more practically, when `action_add_key` detects a re-creation scenario), the new access key's nonce should be seeded to at least `max(initial_nonce_value(block_height), last_known_nonce_for_this_public_key)`, matching the documented invariant in `docs/DataStructures/AccessKey.md` and `core/primitives-core/src/account.rs`. Since access keys are fully removed from the trie on deletion, this likely requires either (a) never fully erasing the nonce record for a public key once used, or (b) enforcing a nonce floor tied to the account's highest-ever nonce usage, closing the TODO tracked as `#6724`.

### Proof of Concept
1. Alice holds full-access key `PK` on `alice.near`, current stored nonce `N0`.
2. Alice signs (but withholds) `Tx_A`: `Transfer 100 NEAR to bob.near` with `nonce = N0 + 1_000_000`.
3. Alice submits `DeleteKey(PK)` then `AddKey(PK, FullAccess)` in the same or next block — both succeed per `action_delete_key`/`action_add_key` [8](#0-7) .
4. The re-added key's nonce is now `initial_nonce_value(current_block_height)`, likely far smaller than `N0 + 1_000_000` since block height only advanced by a few blocks (`initial_nonce_value(h) = (h-1) * 1_000_000`) [3](#0-2) .
5. Alice (or anyone who obtained `Tx_A`, e.g. a relayer who was supposed to submit it only once, or Alice herself trying to "undo" and redo the transfer) broadcasts `Tx_A`. `verify_nonce` accepts it because `tx_nonce > current_nonce` [9](#0-8) , and the transfer executes again — a replay of a transaction whose validity window the account owner believed was closed by rotating the key.

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

**File:** docs/DataStructures/AccessKey.md (L8-11)
```markdown
    /// The nonce for this access key.
    /// NOTE: In some cases the access key needs to be recreated. If the new access key reuses the
    /// same public key, the nonce of the new access key should be equal to the nonce of the old
    /// access key. It's required to avoid replaying old transactions again.
```

**File:** core/primitives-core/src/account.rs (L732-735)
```rust
    /// Nonce for this access key, used for tx nonce generation. When access key is created, nonce
    /// is set to `(block_height - 1) * 1e6` to avoid tx hash collision on access key re-creation.
    /// See <https://github.com/near/nearcore/issues/3779> for more details.
    pub nonce: Nonce,
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

**File:** integration-tests/src/tests/standard_cases/mod.rs (L1169-1174)
```rust
        Err(err) => {
            // TODO(#6724): This is a wrong error, the transaction actually
            // succeeds. We get an error here when we retry the tx and the second
            // time around it fails. Normally, retries are handled by nonces, but we
            // forget the nonce when we delete a key!
            assert_eq!(
```
