### Title
Access-key nonce reseed on self `DeleteKey`+`AddKey` allows replay of an already-executed transaction, breaking exactly-once execution and double-spending its bundled action - ([File: runtime/runtime/src/access_keys.rs])

### Summary
`initial_nonce_value(block_height) = (block_height-1) * 1_000_000` reseeds an access key's nonce whenever it is (re-)created, based only on the *current* `apply_state.block_height` [1](#0-0) . When a transaction bundles a valuable action together with `DeleteKey`+`AddKey` for the **same** public key that signed it, the reseed can land *below* the very nonce that just authorized that transaction, because both events occur at the same `block_height`. That lets the identical signed transaction be resubmitted at a later block and validated again by `verify_nonce`, causing its other actions to execute a second time.

### Finding Description
`action_add_key` seeds a freshly (re-)created regular key's nonce via `add_regular_key`, using `apply_state.block_height` at the time the *add* action is executed [2](#0-1) . `action_delete_key`/`add_regular_key` do not consider what nonce was already consumed by the very transaction performing the delete+re-add — they only look at the current block height.

Exploit flow (attacker acts entirely on their own account with an unprivileged full-access key):
1. At block `H0`, the attacker adds a fresh key `K` to their own account (`AddKey`). Its nonce is seeded to `(H0-1)*1_000_000` [3](#0-2) .
2. At block `H` (can be the very same block/next receipt), the attacker signs one transaction `T`, using `K`, with `nonce = (H-1)*1_000_000 + 1` and an action list `[FunctionCall(victim_contract, "claim"/"deposit", ...), DeleteKey(K), AddKey(K, FullAccess)]`.
3. `verify_nonce` validates `T`: `tx_nonce > current_nonce` and `tx_nonce < block_height*1_000_000` both hold [4](#0-3) . `T` executes: the `FunctionCall` action runs, then `DeleteKey` removes `K`, then `AddKey` re-adds `K`, reseeding its nonce to `initial_nonce_value(H) = (H-1)*1_000_000` — exactly one less than `T`'s own nonce, because the delete+re-add executed at the **same** `block_height` as the tx that consumed that nonce.
4. At any later block `H2 > H` (within `transaction_validity_period` of `T`'s referenced block hash, checked in `check_transaction_validity_period` [5](#0-4) ), the attacker resubmits the byte-identical `T` to any RPC node. `verify_nonce` passes again (`(H-1)*1_000_000+1 > (H-1)*1_000_000`, and `< H2*1_000_000`), so `T` is admitted and executed a second time.

The only anti-replay/dedup mechanism observed is a **per-chunk** hash set (`seen_tx_hashes`), which only prevents the identical hash appearing twice *within one chunk* under `UniqueChunkTransactions` [6](#0-5) ; it does not persist across chunks/blocks. Nonce monotonicity is otherwise the sole cross-block replay defense, and this bug defeats it specifically for self `DeleteKey`+`AddKey` bundles.

The existing regression test for the nonce-reseed anti-collision mechanism (`test_transaction_hash_collision`) only exercises the case where the account is deleted and recreated many blocks later with a *tiny* original nonce, so the reseed value is far above the old nonce and the replay is correctly rejected [7](#0-6) . It does not cover the attacker-chosen-nonce, same-block-height case described above.

### Impact Explanation
This is a protocol-level double-spend/replay primitive: an attacker can force the network to execute one signed transaction's non-`AddKey`/`DeleteKey` actions twice while paying transaction/network fees for only one extra low-cost resubmission. Any bundled action whose value to the attacker is not fully offset by a repeated debit (e.g., a zero/low-deposit `FunctionCall` to a reward/airdrop/faucet/staking contract that trusts the chain's exactly-once nonce guarantee instead of its own explicit anti-replay bookkeeping) is executed twice, letting the attacker extract double value from the counterparty contract/account. This falls under "double-spend/replay" impact.

### Likelihood Explanation
Fully reachable by an unprivileged account: the attacker only needs to control their own account and key, add a key, and craft one transaction bundling a value-generating action with a self `DeleteKey`+`AddKey`. No validator, node operator, or special privilege is required. The nonce value needed (`(block_height-1)*1_000_000 + 1`, i.e. one more than the just-created key's initial nonce) is trivially satisfiable — it is in fact the smallest usable nonce for a freshly added key. Repeatable at will against any counterparty contract that assumes a NEAR transaction/receipt executes exactly once.

### Recommendation
Do not reseed a regular/gas key's nonce purely from `apply_state.block_height` when the delete+re-add originates from the same receipt/transaction whose own nonce may already exceed that seed. Track and persist the maximum nonce ever observed for a given `(account_id, public_key)` pair independent of key deletion (e.g., store a monotonic "high-water mark" separate from the access key record, or require the new seed to be `max((block_height-1)*1_000_000, previous_max_nonce_ever_used_by_this_pubkey + 1)`), so a re-added key can never accept a nonce that was already valid for a prior incarnation of that key within the same block.

### Proof of Concept
Runtime/apply integration test outline (mirrors existing tests in `runtime/runtime/src/tests/apply.rs`):
1. Create an account `alice` with a full-access key `K_admin` and sufficient balance; deploy a simple "counter"/"claim" contract as `victim` with a method `claim` that increments a counter/pays out on each call (no internal idempotency check).
2. Using `K_admin`, submit `AddKey(K_attack, FullAccess)` for `alice`, applied at block `H`.
3. Compute `n = initial_nonce_value(H) + 1`. Build `T = SignedTransaction` signed by `K_attack`, `nonce = n`, actions `= [FunctionCall(victim, "claim", 0 deposit), DeleteKey(K_attack), AddKey(K_attack, FullAccess)]`, applied in the same block `H` (or the next block).
4. Assert `T` succeeds and the `victim` contract's counter increments once; assert the new stored `access_key.nonce` for `K_attack` equals `initial_nonce_value(H)`, which is `< n`.
5. Advance a few blocks (`H2 > H`, within `transaction_validity_period`), resubmit the identical `T` bytes via `apply`/`process_tx`.
6. Assert `T` is accepted (`ProcessTxResponse::ValidTx` / `verify_nonce` succeeds) and executes successfully again, incrementing the `victim` counter a second time — demonstrating double execution of the same signed transaction.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
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

**File:** runtime/runtime/src/verifier.rs (L210-236)
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
```

**File:** chain/chain/src/store/utils.rs (L56-75)
```rust
pub fn check_transaction_validity_period(
    chain_store: &ChainStoreAdapter,
    prev_block_header: &BlockHeader,
    base_block_hash: &CryptoHash,
    transaction_validity_period: BlockHeightDelta,
) -> Result<(), InvalidTxError> {
    let base_header =
        chain_store.get_block_header(base_block_hash).map_err(|_| InvalidTxError::Expired)?;

    metrics::CHAIN_VALIDITY_PERIOD_CHECK_DELAY
        .observe(prev_block_header.height().saturating_sub(base_header.height()) as f64);

    // First check the distance between blocks
    if prev_block_header.height() > base_header.height() + transaction_validity_period {
        return Err(InvalidTxError::Expired);
    }

    // Then check if there is a path between the blocks (`base` is an ancestor of `prev`)
    validity_period_validate_is_ancestor(&base_header, prev_block_header, chain_store)
}
```

**File:** runtime/runtime/src/lib.rs (L2029-2042)
```rust
        let (maybe_expired_txs, _) =
            signed_txs.get_potentially_expired_transactions_and_expiration_flags();
        let skip_duplicate_txs = ProtocolFeature::UniqueChunkTransactions.enabled(protocol_version);
        let mut seen_tx_hashes = HashSet::with_capacity(num_transactions);
        let mut num_skipped_duplicate_txs = 0;
        for (tx, maybe_validation_error) in maybe_expired_txs.iter().zip(validations) {
            // A transaction hash is its outcome id, and outcomes are committed
            // keyed by that id. Processing the same hash twice would commit two
            // conflicting outcomes under one id, so skip any repeat occurrence.
            if skip_duplicate_txs && !seen_tx_hashes.insert(*tx.hash()) {
                tracing::debug!(tx_hash = ?tx.hash(), "skipping duplicate transaction in chunk");
                num_skipped_duplicate_txs += 1;
                continue;
            }
```

**File:** integration-tests/src/tests/features/access_key_nonce_for_implicit_accounts.rs (L47-110)
```rust
/// Test that duplicate transactions are properly rejected.
#[test]
fn test_transaction_hash_collision() {
    let epoch_length = 5;
    let mut genesis = Genesis::test(vec!["test0".parse().unwrap(), "test1".parse().unwrap()], 1);
    genesis.config.epoch_length = epoch_length;
    genesis.config.transaction_validity_period = epoch_length * 2;
    let mut env = TestEnv::builder(&genesis.config).nightshade_runtimes(&genesis).build();
    let genesis_block = env.clients[0].chain.get_block_by_height(0).unwrap();

    let signer0 = InMemorySigner::test_signer(&"test0".parse().unwrap());
    let signer1 = InMemorySigner::test_signer(&"test1".parse().unwrap());
    let send_money_tx = SignedTransaction::send_money(
        1,
        "test1".parse().unwrap(),
        "test0".parse().unwrap(),
        &signer1,
        Balance::from_yoctonear(100),
        *genesis_block.hash(),
    );
    let delete_account_tx = SignedTransaction::delete_account(
        2,
        "test1".parse().unwrap(),
        "test1".parse().unwrap(),
        "test0".parse().unwrap(),
        &signer1,
        *genesis_block.hash(),
    );

    assert_eq!(
        env.rpc_handlers[0].process_tx(send_money_tx.clone(), false, false),
        ProcessTxResponse::ValidTx
    );
    assert_eq!(
        env.rpc_handlers[0].process_tx(delete_account_tx, false, false),
        ProcessTxResponse::ValidTx
    );

    for i in 1..4 {
        env.produce_block(0, i);
    }

    let create_account_tx = SignedTransaction::create_account(
        1,
        "test0".parse().unwrap(),
        "test1".parse().unwrap(),
        Balance::from_near(1),
        signer1.public_key(),
        &signer0,
        *genesis_block.hash(),
    );
    assert_eq!(
        env.rpc_handlers[0].process_tx(create_account_tx, false, false),
        ProcessTxResponse::ValidTx
    );
    for i in 4..8 {
        env.produce_block(0, i);
    }

    assert_matches!(
        env.rpc_handlers[0].process_tx(send_money_tx, false, false),
        ProcessTxResponse::InvalidTx(_)
    );
}
```
