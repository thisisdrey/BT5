### Title
Gas-key nonce reseed via DeleteKey+AddKey does not dominate a same-block high-water-mark, enabling transaction replay - (File: runtime/runtime/src/access_keys.rs)

### Summary
`add_gas_key` reseeds every gas-key nonce slot to `initial_nonce_value(block_height) = (block_height-1) * 1_000_000` unconditionally, without ever reading the nonce value that was in effect before deletion. When a gas key is used at the same block height at which it is later deleted and recreated, the reseeded nonce is strictly lower than the nonce value that was already consumed at that height, allowing a previously-executed gas-key transaction to be legally resubmitted and re-applied.

### Finding Description
`add_gas_key` (`runtime/runtime/src/access_keys.rs:194-228`) writes every nonce index with:
```
let nonce = initial_nonce_value(block_height);
for i in 0..num_nonces { set_gas_key_nonce(..., i, nonce); }
```
`initial_nonce_value` (`access_keys.rs:46-50`) is `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (`1_000_000`). This value is chosen only from the current block height; it never considers the actual nonce that was live on the key before `action_delete_key`/`delete_gas_key` wiped the nonce entries (`access_keys.rs:93-134`).

`verify_nonce` (`runtime/runtime/src/verifier.rs:211-237`) allows any `tx_nonce` up to (but not including) `block_height * 1_000_000` for that block:
```
let upper_bound = height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER;
if tx_nonce >= upper_bound { return Err(NonceTooLarge...) }
```
So a gas-key transaction executed at height `H` can legally carry `tx_nonce` up to `H*1e6 - 1`, and that value is written back into the per-index nonce slot on success.

If, within the *same chunk/height* `H`, the account owner also submits a batched `[DeleteKey, AddKey]` transaction re-creating the same gas key, `add_gas_key` reseeds the nonce slot to `(H-1)*1e6` — which is smaller than the `H*1e6 - 1` value that a use-transaction at height `H` could have just written. This is exactly the "collision boundary" `H_recreate <= H_use`: the reseed does not dominate the historical high-water mark, it re-enters below it.

Because gas-key nonces are the *only* replay-protection mechanism (`verify_and_charge_gas_key_tx_ephemeral`, `verifier.rs:370-419`, uses `verify_nonce` exactly like the regular path), lowering the stored nonce below a value that was already consumed makes that old value valid again for a subsequent block. `process_transactions` (`runtime/runtime/src/lib.rs:2031-2042`) only deduplicates identical transaction hashes *within the same chunk* (`ProtocolFeature::UniqueChunkTransactions`, `skip_duplicate_txs`/`seen_tx_hashes`); it performs no cross-chunk/cross-block replay tracking. Consequently, the identical, already-executed transaction (same signature, same `tx_nonce`) can be resubmitted in a *later* chunk (within the tx's block-hash validity window) and will pass `verify_nonce` a second time, causing its actions (e.g. a `Transfer` whose `deposit_cost` is drawn from `account.amount`, per the gas-key cost-split logic in `verify_and_charge_gas_key_tx_ephemeral`) to be executed a second time.

### Impact Explanation
This breaks the nonce-based replay-protection invariant for gas keys: a transaction that has already been included and executed can be re-applied, re-executing its actions (e.g. re-crediting a `Transfer`, re-invoking a `FunctionCall`) a second time using funds from the signer's own account balance. This is a double-spend/replay of an already-settled transaction — the exact category called out in scope ("double-spend/replay"). It also produces two execution outcomes for the same transaction hash across different blocks, an outcome-id collision that the `UniqueChunkTransactions` mechanism was specifically designed to prevent within a chunk but does not prevent across chunks.

### Likelihood Explanation
Preconditions are fully attacker-controlled and require no privileged capability: a funded account with a gas key (`AddKey` with `GasKeyFullAccess`/`GasKeyFunctionCall`), one gas-key-signed "use" transaction with `tx_nonce` chosen close to the per-block upper bound (`H*1e6 - 1`), and a `[DeleteKey, AddKey]` batch transaction re-adding the same gas key. The only non-deterministic factor is landing both transactions in the same chunk height `H`, which the attacker can retry cheaply (their own two transactions, same signer, submitted back-to-back) until it lands; it does not require any block/chunk-production privilege, only ordinary transaction submission. The replay window is bounded by the transaction validity/TTL window, but that is a short, fixed, and generally sufficient window for an attacker controlling both the original and replayed transaction timing.

### Recommendation
`add_gas_key` should seed (or re-seed on recreation) each nonce index to `max(initial_nonce_value(block_height), previous_high_water_mark_if_any)`, or, simpler, always require the new nonce baseline to be strictly greater than any nonce ever observed for that `(account_id, public_key_slot)`. Since public keys are attacker-chosen and reusable, this requires either: (a) never fully deleting per-index nonce state on `delete_gas_key`, keeping a tombstoned high-water mark keyed by `(account_id, public_key)` that `add_gas_key` must respect on recreation, or (b) binding gas-key `tx_nonce` upper bound tighter than `block_height*1e6` so that no single block can produce a nonce that a later reseed at that same height could ever regress below (e.g., reserve/skip an epoch or otherwise guarantee `initial_nonce_value` strictly exceeds any nonce obtainable in the same block).

### Proof of Concept
Unit/integration test in `runtime/runtime/src/tests/apply.rs` style:
1. Create account `alice`, add gas key `K` with `num_nonces = 1` at block height `H`.
2. Fund `alice`'s account balance sufficiently; build a gas-key transaction `TxA` signed by `K`, `nonce_index = 0`, `tx_nonce = H*1_000_000 - 1`, action `Transfer{deposit: X}` to `bob`. Apply at block height `H`; assert success and `bob`'s balance increased by `X`.
3. In the same chunk/apply call at height `H`, include a batched transaction `[DeleteKey(K), AddKey(K, GasKeyFullAccess(1))]` signed by `alice`'s full-access key, executed after `TxA`. Assert it succeeds and the nonce slot for `K`,`0` is now `initial_nonce_value(H) = (H-1)*1_000_000`.
4. Assert `(H-1)*1_000_000 < H*1_000_000 - 1` (the collision), i.e. `new_nonce(0) < old_nonce(0)`.
5. At a later block height `H+1` (within TTL), resubmit the *identical* `TxA` bytes/hash. Assert `verify_nonce`/`verify_and_charge_gas_key_tx_ephemeral` accepts it (no `InvalidNonce`), the transfer is re-applied, and `bob`'s balance increases by `X` a second time — demonstrating double execution of an already-settled transaction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L194-215)
```rust
fn add_gas_key(
    fee_config: &RuntimeFeesConfig,
    state_update: &mut TrieUpdate,
    account: &mut Account,
    account_id: &AccountId,
    public_key: &PublicKey,
    access_key: &AccessKey,
    gas_key_info: &GasKeyInfo,
    block_height: BlockHeight,
) -> Result<(), StorageError> {
    // For gas keys, nonce stored on access key is not used and should always be zero
    let mut access_key = access_key.clone();
    access_key.nonce = 0;
    set_access_key(state_update, account_id.clone(), public_key.clone(), &access_key);

    // Set up nonces for gas key
    let num_nonces = gas_key_info.num_nonces;
    let nonce = initial_nonce_value(block_height);
    for i in 0..num_nonces {
        set_gas_key_nonce(state_update, account_id.clone(), public_key.clone(), i, nonce);
    }

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

**File:** runtime/runtime/src/verifier.rs (L370-447)
```rust
pub fn verify_and_charge_gas_key_tx_ephemeral(
    config: &RuntimeConfig,
    account: &Account,
    access_key: &AccessKey,
    current_nonce: Nonce,
    tx: &Transaction,
    transaction_cost: &TransactionCost,
    block_height: Option<BlockHeight>,
    pending: &PendingConstraints,
) -> TxVerdict {
    // It's the caller's responsibility to ONLY call this function for transactions with
    // nonce_index (i.e. gas key transactions).
    let Some(nonce_index) = tx.nonce().nonce_index() else {
        panic!("verify_and_charge_gas_key_tx_ephemeral called for non-gas key transaction")
    };
    let TransactionCost {
        gas_burnt,
        compute_burnt,
        gas_remaining,
        receipt_gas_price,
        burnt_amount,
        gas_cost,
        deposit_cost,
        ..
    } = *transaction_cost;
    let account_id = tx.signer_id();

    // Validate that access key is a gas key
    let Some(gas_key_info) = access_key.gas_key_info() else {
        return TxVerdict::Failed(InvalidTxError::InvalidAccessKeyError(
            InvalidAccessKeyError::AccessKeyNotFound {
                account_id: account_id.clone(),
                public_key: Box::new(tx.public_key().clone()),
            },
        ));
    };

    // Validate nonce_index is in valid range
    if nonce_index >= gas_key_info.num_nonces {
        return TxVerdict::Failed(InvalidTxError::InvalidNonceIndex {
            tx_nonce_index: Some(nonce_index),
            num_nonces: gas_key_info.num_nonces,
        });
    }

    let tx_nonce = tx.nonce().nonce();
    let effective_nonce = std::cmp::max(current_nonce, pending.max_nonce);
    if let Err(e) = verify_nonce(tx_nonce, effective_nonce, block_height, tx.nonce_mode()) {
        return TxVerdict::Failed(e);
    }

    // Check gas key has enough balance for gas costs, accounting for
    // pending gas key costs (prior gas key txs + pending WithdrawFromGasKey).
    // Unlike account balance, gas key balance only changes through transactions
    // that PTQ explicitly tracks, so pending should never exceed the balance.
    let Some(available_gas_key_balance) =
        gas_key_info.balance.checked_sub(pending.paid_from_gas_key)
    else {
        tracing::error!(
            target: "runtime",
            balance = %gas_key_info.balance,
            paid_from_gas_key = %pending.paid_from_gas_key,
            "pending gas key costs exceed gas key balance"
        );
        return TxVerdict::Failed(InvalidTxError::NotEnoughGasKeyBalance {
            signer_id: account_id.clone(),
            balance: Balance::ZERO,
            cost: gas_cost,
        });
    };
    if available_gas_key_balance < gas_cost {
        return TxVerdict::Failed(InvalidTxError::NotEnoughGasKeyBalance {
            signer_id: account_id.clone(),
            balance: available_gas_key_balance,
            cost: gas_cost,
        });
    }
    let new_gas_key_balance = gas_key_info.balance.checked_sub(gas_cost).unwrap();
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
