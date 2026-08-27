### Title
Nonce regression via same-receipt DeleteKey+AddKey reset (`initial_nonce_value` ignores prior nonce) - ([File: runtime/runtime/src/access_keys.rs])

### Summary
`action_add_key` treats a public key as brand-new whenever `get_access_key` returns `None` at that point in the receipt's action list, including a key that was just deleted earlier in the *same* action batch. The freshly-added key's nonce is reset purely from `initial_nonce_value(block_height)`, with no floor against the highest nonce ever consumed by that key. Because a signer can legitimately advance a key's nonce far ahead of `(block_height-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (up to the verification-time upper bound of `block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`), a single transaction containing `[DeleteKey(pk), AddKey(pk, ...)]` can push the tx's own nonce high, then collapse the stored nonce back down within the same block, reviving a window of previously-issued nonce values.

### Finding Description
`action_delete_key` (`runtime/runtime/src/access_keys.rs:52-91`) calls `remove_access_key` on the mutable `state_update`. Within the same action receipt, `apply_action_receipt` executes actions sequentially against the same `TrieUpdate` (`runtime/runtime/src/lib.rs:892-951`), so a subsequent `AddKey` action in the same action list sees the change immediately.

`action_add_key` (`runtime/runtime/src/access_keys.rs:149-192`) only rejects an add if `get_access_key(...).is_some()`: [1](#0-0) 
Since the key was just removed by the preceding `DeleteKey` action in the same batch, this check passes, and `add_regular_key` resets the nonce unconditionally: [2](#0-1) 
using [3](#0-2) 
`initial_nonce_value` depends *only* on the current block height, not on the nonce value the key had immediately before deletion.

Meanwhile, `verify_nonce` (`runtime/runtime/src/verifier.rs:210-237`) enforces only `tx_nonce > current_nonce` and `tx_nonce < block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` — it allows a signer to jump their own key's nonce arbitrarily high within the current block-height window in a single transaction, since `tx_cost`/nonce validation places no restriction on the *magnitude* of the jump beyond the multiplier ceiling.

Exploit flow, all inside a **single transaction** signed by the key owner, with actions `[DeleteKey(pk), AddKey(pk, new_permission)]` and a transaction-level nonce `tx_nonce` chosen close to the block's upper bound (`block_height * MULTIPLIER - 1`):
1. At transaction-processing time (block height `H`), `verify_and_charge_tx_ephemeral` accepts `tx_nonce` (since it merely needs to exceed the previous, lower `current_nonce`) and writes `access_key.nonce = tx_nonce` to state.
2. The resulting local receipt executes its actions in order against the same `TrieUpdate`: `DeleteKey(pk)` removes the key, then `AddKey(pk, ...)` re-adds it and calls `initial_nonce_value(H) = (H-1) * MULTIPLIER`, which is far below `tx_nonce` (up to almost `MULTIPLIER - 1` lower).
3. After this transaction, the on-chain stored nonce for `pk` is `(H-1)*MULTIPLIER`, even though a nonce as high as `tx_nonce` was already consumed/observed as "used" by anyone monitoring the chain (e.g., a relayer, meta-transaction service, or counterparty that treats nonce advancement as irrevocable cancellation of any pending signed transaction with a lower nonce).
4. Any previously signed transaction (or NEP-366 `DelegateAction`) for `pk` with a nonce in `[(H-1)*MULTIPLIER, tx_nonce)` — which the signer or a counterparty believed permanently invalidated by the nonce jump in step 1 — is now valid again and can be submitted/relayed for execution.

None of the existing checks (`AddKeyAlreadyExists`, `verify_nonce`, storage-staking, signature) prevent this, because they were not designed to track a "highest nonce ever seen" independent from the currently-stored access key nonce; `initial_nonce_value` was designed only to avoid the block-height-based tx-hash collision described in near/nearcore#3779, not to guard against a same-block reset undercutting an already-consumed nonce.

### Impact Explanation
This is a replay/double-spend primitive: a transaction (e.g., a meta-transaction/`DelegateAction` payment, escrow release, or exchange order) that the signer or a relying party considered invalidated (superseded by a higher nonce) can be resurrected and executed after the signer resets their own key's nonce via `DeleteKey`+`AddKey` in one transaction. Any party (protocol, relayer, counterparty) that treats "nonce advanced past X" as an irrevocable guarantee that transaction X can never execute is exposed to double-spending or unintended fund movement — the category is double-spend/replay of signed transactions.

### Likelihood Explanation
The only precondition is a full-access (or otherwise key-management-capable) key on the attacker's own account — something any unprivileged NEAR account holder has. The attack is a single, deterministically constructible transaction: choose a high `tx_nonce`, include `[DeleteKey(pk), AddKey(pk, ...)]`. No validator, relayer, or third-party cooperation is required to *trigger* the state change; only realizing "impact" requires a pre-existing, previously-signed lower-nonce transaction that a counterparty/relayer still holds and is willing to (re)submit believing it stale. This is fully repeatable and costs only standard gas fees.

### Recommendation
When re-adding a key with the same public key that existed (or was deleted) earlier in the same account's lifetime, the new nonce must never be lower than the highest nonce that key has ever reached. Concretely, in `add_regular_key`/`add_gas_key`, compute the new nonce as `max(initial_nonce_value(block_height), previous_nonce_before_delete_in_this_batch)`, or track a per-`(account_id, public_key)` "high-water mark" nonce in state that survives `DeleteKey` and is consulted by `AddKey`, so a delete+re-add within the same receipt (or across receipts) can never regress below any nonce value the key has already exposed as "current."

### Proof of Concept
Unit test in `runtime/runtime/src/access_keys.rs` (or an integration test in `runtime/runtime/src/tests/apply.rs`):
1. Set up an account with a full-access key `pk`, `AccessKey.nonce = 0`, at `block_height = H`.
2. Simulate normal usage advancing `access_key.nonce` to a high value close to `H * ACCESS_KEY_NONCE_RANGE_MULTIPLIER - 1` (mirroring what `verify_and_charge_tx_ephemeral` would accept).
3. Build a receipt with actions `[DeleteKeyAction { public_key: pk }, AddKeyAction { public_key: pk, access_key: AccessKey::full_access() }]` and run it through `apply_action_receipt`/`action_delete_key` + `action_add_key` at the same `block_height = H`.
4. Assert that the access key stored after the receipt (`get_access_key`) has `nonce == initial_nonce_value(H)`, which is strictly less than the nonce set in step 2 — demonstrating the regression.
5. Follow-up: construct a `SignedTransaction` with a nonce between `initial_nonce_value(H)` and the step-2 high nonce, and show `verify_and_charge_tx_ephemeral`/`validate_verify_and_charge_transaction` now accepts it (`Ok`), where before the reset it would have failed with `InvalidTxError::InvalidNonce`.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L156-164)
```rust
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

**File:** runtime/runtime/src/access_keys.rs (L238-241)
```rust
) -> Result<(), StorageError> {
    let mut access_key = access_key.clone();
    access_key.nonce = initial_nonce_value(block_height);
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);
```
