### Title
Access-key delete+recreate nonce reseed can undershoot an already-consumed same-block nonce, enabling transaction replay - (File: runtime/runtime/src/access_keys.rs / runtime/runtime/src/verifier.rs)

### Summary
`initial_nonce_value` reseeds a recreated access key's nonce to `(block_height-1)*ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, while `verify_nonce`'s upper-bound check only requires `tx_nonce < block_height*ACCESS_KEY_NONCE_RANGE_MULTIPLIER`. When a signer submits a high-nonce transaction and, within the *same* block/chunk, deletes and re-adds the same public key, the reseeded nonce can be lower than the nonce already consumed by the earlier transaction, allowing that earlier transaction's exact bytes to pass `verify_nonce` again in a later block.

### Finding Description
`initial_nonce_value(block_height)` sets a freshly (re)created regular key's nonce to `(block_height-1) * AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (`M = 1_000_000`): [1](#0-0) 

This value is written by `add_regular_key` on `action_add_key`: [2](#0-1) 

`verify_nonce` enforces monotonicity plus an upper bound tied to the *current* block height of the chunk being applied: [3](#0-2) 

Both `tx1` (the original transaction) and `tx2` (DeleteKey+AddKey) are validated with the **same** `block_height = H` when included in the same chunk. This means:
- `tx1`'s nonce `X` only needs to satisfy `X < H*M` (it can be as large as `H*M - 1`).
- After `tx2` runs `action_delete_key` (removes the key) followed by `action_add_key` for the same public key, the new nonce is seeded to `(H-1)*M`.
- Since `X` can be up to `H*M - 1` while the new seed is `(H-1)*M`, the reseeded nonce can be far lower (by up to `M-1`) than the nonce already consumed by `tx1`.

Because both actions are in one receipt/transaction with the account acting as its own full-access predecessor, `action_delete_key` and `action_add_key` execute sequentially without re-checking the specific access key mid-receipt.

Exploit flow:
1. Signer crafts `tx1` (arbitrary action, e.g. `FunctionCall`/`Transfer`) with `nonce = X` close to `H*M - 1`, and `tx2 = [DeleteKey(pk), AddKey(pk, FullAccess)]` with `nonce = X+1 < H*M`.
2. Both are submitted so the chunk producer includes them in the same chunk at height `H`. `tx1` executes, advancing `access_key.nonce` to `X`. `tx2` executes, deleting then recreating the key, resetting `access_key.nonce` to `(H-1)*M`, which is `< X`.
3. At a later block `H' > H` (within `transaction_validity_period` and the block-hash reference window of `tx1`), the signer (or anyone with the bytes) resubmits the original `tx1` bytes unchanged. `verify_nonce` now checks `X > (H-1)*M` (true) and `X < H'*M` (true), so the stale, already-executed transaction passes nonce validation again and is converted into a new receipt, re-executing `tx1`'s action.
4. There is no additional replay protection: the only chunk-level duplicate-hash guard (`UniqueChunkTransactions`) only dedups within a single `apply()` call for one chunk, not across chunks/blocks: [4](#0-3) 
This confirms nonce monotonicity is the *sole* replay-protection mechanism across blocks, and the seeding logic (motivated by near/nearcore#3779) fails to preserve that guarantee when key deletion/recreation happens in the same block height as the nonce being protected against.

### Impact Explanation
This is a protocol-level nonce/replay-protection defect: a validly-signed, already-executed transaction can be re-executed once, producing a duplicate receipt for the same signed bytes. For a `Transfer` action this double-debits the signer's own balance (self-harm, low value), but for a `FunctionCall` to any contract whose logic is not itself idempotent and relies on NEAR's nonce/replay guarantee (e.g., "claim once" logic, airdrops, unlocks), a duplicate execution constitutes an unintended double-spend / replay of state-changing effects against that contract — matching the "double-spend/replay" bounty category.

### Likelihood Explanation
Exploitation requires: (1) the attacker fully controls their own account, its access key, and the nonce values chosen for both transactions (all within reach of an ordinary funded account); (2) the attacker must get `tx1` (high nonce, close to `H*M-1`) and `tx2` (DeleteKey+AddKey same public key) included in the exact same chunk/block height — achievable in practice by submitting both back-to-back before the next block is produced, since nothing prevents multiple transactions from one account landing in the same chunk; (3) the replay resubmission must occur within `transaction_validity_period` and while `tx1`'s referenced block hash is still valid, which is trivially satisfiable by resubmitting promptly. No validator, RPC operator, or other privileged access is required — this is entirely within reach of a normal signer/client.

### Recommendation
Change the nonce-upper-bound check and/or the reseed formula so they cannot disagree within the same block: e.g., seed a recreated key's nonce to `block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (matching, not one less than, the current block's upper bound) instead of `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, or track and enforce a strictly-increasing "highest nonce ever seen" per (account, public_key) that survives DeleteKey (independent of the AccessKey record) so recreation can never regress below a previously consumed nonce.

### Proof of Concept
Runtime integration test (extending the pattern of `test_duplicate_transaction_in_chunk_skipped`/`test_transaction_hash_collision`) using two separate `Runtime::apply` calls to simulate two chunks at heights `H` and `H' > H`:
1. Set up `alice_account()` with a full-access key `pk`, at block height `H`.
2. Build `tx1`: a `Transfer` (or `FunctionCall`) from `alice` to `bob`/a target contract, with `nonce = H*M - 2`.
3. Build `tx2`: `[DeleteKeyAction{pk}, AddKeyAction{pk, FullAccess}]`, with `nonce = H*M - 1`.
4. Call `runtime.apply(..., apply_state.block_height = H, signed_txs = [tx1, tx2], ...)`. Assert both succeed; assert new `access_key.nonce == (H-1)*M` and confirm `(H-1)*M < H*M - 2`.
5. Commit resulting trie root; construct `apply_state` for height `H' = H+1` (or a few blocks later, within validity period).
6. Call `runtime.apply(..., apply_state.block_height = H', signed_txs = [tx1.clone()], ...)` (the exact same `tx1` bytes/hash).
7. Assert the outcome for `tx1`'s hash is `SuccessReceiptId` a second time (not `InvalidNonce`), and assert the receiver's balance / contract state reflects the action having been applied twice — demonstrating the value-conservation/no-double-execution invariant is violated.

### Citations

**File:** runtime/runtime/src/access_keys.rs (L46-50)
```rust
pub(crate) fn initial_nonce_value(block_height: BlockHeight) -> Nonce {
    // Set default nonce for newly created access key to avoid transaction hash collision.
    // See <https://github.com/near/nearcore/issues/3779>.
    (block_height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER
}
```

**File:** runtime/runtime/src/access_keys.rs (L230-255)
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

    account.set_storage_usage(
        account
            .storage_usage()
            .checked_add(access_key_storage_usage(fee_config, public_key, &access_key))
            .ok_or_else(|| {
                StorageError::StorageInconsistentState(format!(
                    "Storage usage integer overflow for account {}",
                    account_id
                ))
            })?,
    );
    Ok(())
}
```

**File:** runtime/runtime/src/verifier.rs (L210-237)
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
