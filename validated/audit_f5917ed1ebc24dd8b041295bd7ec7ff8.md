### Title
Access-key delete/re-add nonce reseed can dip below a prior nonce high-water-mark, enabling transaction replay - (File: runtime/runtime/src/access_keys.rs, runtime/runtime/src/verifier.rs)

### Summary
`initial_nonce_value` seeds a freshly (re-)added access key's nonce purely from `(block_height-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, with no awareness of any nonce previously consumed by an earlier incarnation of the same `(account_id, public_key)` pair. Because `verify_nonce`'s upper-bound check is also computed from the *current* block height and `NonceMode::Monotonic` permits jumping the nonce to any value below that bound in a single transaction, an attacker who deletes and re-adds the same public key within the same chunk as a high-nonce transaction can reset the key's nonce watermark below a value that was already consumed, enabling replay of that transaction (or any tx nonce in the reopened window).

### Finding Description
`initial_nonce_value` [1](#0-0)  computes the seed nonce solely from the *current* `block_height`, with no reference to any nonce that a prior incarnation of the same public key on the account may have already reached. `add_regular_key`/`add_gas_key` apply this seed unconditionally on every `AddKey` [2](#0-1) , and `action_delete_key`/`delete_regular_key` fully erase the old `AccessKey` record (including its nonce) from the trie [3](#0-2) . There is no tombstone or persisted high-water-mark kept for a deleted public key.

`verify_nonce`'s upper bound is likewise derived from the block height at the moment of verification, not the height at which the key was created: `upper_bound = height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [4](#0-3) , and `NonceMode::Monotonic` only requires `tx_nonce > current_nonce` [5](#0-4)  — it does not require sequential (`+1`) nonces, so a single transaction may legally jump the nonce almost all the way up to `height * 1_000_000`.

Because `ApplyState::block_height` is fixed once per chunk `apply()` call, every transaction applied in the same chunk shares the same `block_height`. This produces the exploitable condition: if, within one chunk, an account (a) submits a transaction `tx1` signed by key `Kx` that jumps `Kx`'s nonce close to the chunk's upper bound `H*1_000_000`, and then (b) `DeleteKey(Kx)` followed by `AddKey(Kx)` (re-adding the identical public key) are also applied in that same chunk, the re-added key's nonce is reseeded to `(H-1)*1_000_000` — a value strictly below the nonce `tx1` already consumed. A later resubmission of `tx1` (or any transaction with a nonce inside the reopened gap) will pass `verify_nonce`'s `tx_nonce > current_nonce` check and the signature check (same key material), because the runtime has no memory that this nonce range was already used by the deleted incarnation of the key.

This breaks the invariant explicitly documented in the codebase's own spec ("Nonce monotonicity prevents replay", `verifier.rs:212`) — that invariant silently assumes it only needs to hold within a single key's lifetime, not across a delete/re-add cycle of the same public key performed inside a single chunk.

Preconditions for reliable exploitation are that `tx1`, `DeleteKey`, and `AddKey` (or their causal predecessors) land in the *same* chunk in a specific relative order (`tx1` before `DeleteKey`/`AddKey`); the exact ordering of transactions from different signer-key groups within a chunk is determined by the chunk producer/mempool, not directly by the attacker, so this is not deterministically forceable by an unprivileged sender on every attempt, but it is retriable at negligible cost.

### Impact Explanation
If achieved, this permits replay of an already-executed, previously-signed transaction against the *same* account after that account's key has been cycled — i.e., re-execution of an action (e.g., a `Transfer`, `FunctionCall` with attached deposit, or a signed meta-transaction) whose effects were meant to be one-time. This falls under the "double-spend/replay" bounty category. The immediate financial loss is to the account itself (self-inflicted double execution of its own already-consumed nonce range) unless the replayed transaction interacts with an external contract that treats the transaction/receipt as a fresh, uniquely-authorized event (e.g., a bridge/exchange credit, a promise-triggered mint, or a delegate/meta-transaction relay), in which case the attacker could obtain a double credit while their own account state shows only one execution's worth of nonce consumption.

### Likelihood Explanation
The attacker needs only to be an ordinary account holder: sign and submit (1) a transaction with a nonce deliberately jumped close to the chunk's nonce ceiling, (2) `DeleteKey`, and (3) `AddKey` re-adding the same public key, timed so all land in one chunk in the right relative order, then later resubmit the original (or an intermediate-nonce) transaction. No validator, RPC-operator, or leaked-key access is required. The main limiting factor is that same-chunk relative ordering across different signer-key transaction groups is not fully attacker-controlled (it depends on mempool/chunk-producer transaction selection), making success probabilistic rather than guaranteed on a single attempt; however, it is cheap to retry across many blocks, and the underlying defect (nonce reseed with no cross-incarnation watermark) is deterministically demonstrable in a single `runtime.apply()` call with an explicit transaction ordering, as is already done in existing tests like `test_duplicate_transaction_in_chunk_skipped` [6](#0-5) .

### Recommendation
Do not let `AddKey` reseed a public key's nonce based purely on the current block height when a prior incarnation of that exact `(account_id, public_key)` may have consumed a higher nonce. Options: (a) persist a per-`(account_id, public_key)` high-water-mark nonce that survives `DeleteKey` (a tombstone) and take `max(tombstone_nonce, initial_nonce_value(block_height))` when re-adding the key; or (b) forbid reusing the exact same public key on an account after deletion within some safety margin; or (c) tie the upper-bound check in `verify_nonce` to the height at which the specific key was created rather than the current chunk height, closing the "large nonce jump in one chunk" primitive that makes the reseed race practical.

### Proof of Concept
Runtime-level integration test (mirrors `test_duplicate_transaction_in_chunk_skipped` style, using an explicit `SignedValidPeriodTransactions` ordering to deterministically force same-chunk, same-`block_height` execution and sidestep mempool-ordering nondeterminism):
1. Setup account `alice` with existing full-access key `K0` and a second key `Kx` (added via a prior `AddKey`).
2. Build a single chunk with the ordered transaction list, all sharing one `apply_state.block_height = H`:
   - `tx1`: signed by `Kx`, nonce `N = H*1_000_000 - 1` (near the ceiling, legal under `NonceMode::Monotonic`), performing a `Transfer` action with a deposit.
   - `tx2`: `DeleteKey(Kx)` signed by `K0`.
   - `tx3`: `AddKey(Kx)` (re-adding identical public key) signed by `K0`.
3. Apply the chunk; assert `tx1` succeeds and the stored access key nonce for `Kx` becomes `(H-1)*1_000_000` after `tx3` (via `get_access_key`).
4. In a second `apply()` call (subsequent chunk, `block_height = H+1` or later), resubmit `tx1` unchanged (same bytes/hash) or a fresh transaction signed by `Kx` with nonce in `((H-1)*1_000_000, N]`.
5. Assert `verify_nonce`/the resulting `ExecutionOutcome` is `Success`, not `InvalidNonce`, demonstrating the replay/double-execution of the `Transfer` action and confirming the account's balance was debited twice for what was intended as a single authorized transfer.

### Citations

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

**File:** runtime/runtime/src/verifier.rs (L217-222)
```rust
    match nonce_mode {
        NonceMode::Monotonic => {
            if tx_nonce <= current_nonce {
                return Err(InvalidTxError::InvalidNonce { tx_nonce, ak_nonce: current_nonce });
            }
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

**File:** runtime/runtime/src/tests/apply.rs (L4206-4255)
```rust
#[test]
fn test_duplicate_transaction_in_chunk_skipped() {
    let alice_signer = InMemorySigner::test_signer(&alice_account());
    let send_money = |nonce| {
        SignedTransaction::send_money(
            nonce,
            alice_account(),
            bob_account(),
            &alice_signer,
            Balance::from_near(1),
            CryptoHash::default(),
        )
    };
    let tx = send_money(1);
    // A distinct transaction (different nonce, different hash) that must not be skipped.
    let other = send_money(2);
    let (tx_hash, other_hash) = (tx.get_hash(), other.get_hash());
    let (runtime, tries, root, apply_state, _signers, epoch_info_provider) = setup_runtime(
        vec![alice_account(), bob_account()],
        Balance::from_near(1_000_000),
        Balance::from_near(500_000),
        Gas::from_teragas(1000),
    );
    assert!(ProtocolFeature::UniqueChunkTransactions.enabled(PROTOCOL_VERSION));

    // [T, U, T]: the repeat of T is non-adjacent to the original.
    let signed_valid_period_txs =
        SignedValidPeriodTransactions::new(vec![tx.clone(), other, tx], vec![true; 3]);
    let apply_result = runtime
        .apply(
            tries.get_trie_for_shard(ShardUId::single_shard(), root),
            &None,
            &apply_state,
            &[],
            signed_valid_period_txs,
            &epoch_info_provider,
            Default::default(),
        )
        .expect("apply should succeed");

    // The repeat of T is skipped, leaving a single success outcome under its
    // hash rather than a success and a conflicting InvalidNonce failure, while
    // the distinct transaction U is processed normally.
    let tx_outcomes = |id| apply_result.outcomes.iter().filter(|o| o.id == id).collect::<Vec<_>>();
    let (tx_outcomes, other_outcomes) = (tx_outcomes(tx_hash), tx_outcomes(other_hash));
    assert_eq!(tx_outcomes.len(), 1, "duplicate transaction must be skipped");
    assert_matches!(tx_outcomes[0].outcome.status, ExecutionStatus::SuccessReceiptId(_));
    assert_eq!(other_outcomes.len(), 1, "distinct transaction must not be skipped");
    assert_matches!(other_outcomes[0].outcome.status, ExecutionStatus::SuccessReceiptId(_));
}
```
