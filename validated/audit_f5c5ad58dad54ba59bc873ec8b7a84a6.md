### Title
Same-block DeleteKey+AddKey reseeds access-key nonce below an already-consumed nonce, enabling exact transaction replay - (File: runtime/runtime/src/access_keys.rs)

### Summary
`add_regular_key` unconditionally reseeds a newly (re-)added access key's nonce to `initial_nonce_value(block_height) = (block_height-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, without regard to any nonce already consumed by that same public key earlier in the very same block. Because `verify_nonce` allows any `tx_nonce < block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, an attacker can pick a `tx1` nonce close to that upper bound, execute it, then delete+re-add the same key in the same chunk to push the stored nonce back below `tx1`'s nonce, allowing the byte-identical `tx1` to be resubmitted and re-executed in a later block.

### Finding Description
`verify_nonce` (runtime/runtime/src/verifier.rs:211-237) accepts any transaction whose nonce is strictly greater than the access key's current nonce and strictly less than `block_height * AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER`. [1](#0-0) 

`initial_nonce_value` seeds a fresh key's nonce to `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`: [2](#0-1) 

`add_regular_key` applies this seed unconditionally whenever a key with a given public key is (re-)added, with no memory of any nonce previously used by a key with the same public key on this account: [3](#0-2) 

Attack flow, all within block/chunk height `h`:
1. Attacker (unprivileged account owner) signs `tx1` with access key `pk_A` (a `FunctionCall` key restricted to a valuable third-party contract call), choosing `tx1.nonce = h*M - k` for a small `k` (well under the `h*M` upper bound enforced by `verify_nonce`). This is legal because the attacker fully controls nonce selection when signing.
2. `tx1` executes; the signer's `pk_A` access key nonce is persisted as `h*M - k` via the normal nonce-charging path (`set_tx_state_changes`, runtime/runtime/src/verifier.rs:122-131).
3. In the same chunk, the attacker submits `tx2`, signed by a separate `FullAccess` key they legitimately own (`pk_B`), containing `DeleteKey(pk_A)` followed by `AddKey(pk_A, ...)`. Because `pk_A` had only `FunctionCall` permission, `tx2` must use a different, full-access key — which an account owner naturally possesses; no privilege escalation is required.
4. `action_delete_key` removes the access key entry for `pk_A` (runtime/runtime/src/access_keys.rs:52-91), and the subsequent `action_add_key` → `add_regular_key` re-creates `pk_A` with `nonce = (h-1)*M`, which is strictly less than the nonce `h*M - k` already consumed by `tx1` (since `k < M`).
5. In a later block (still within `transaction_validity_period`), the attacker resubmits the byte-identical `tx1`. `verify_nonce` now sees `current_nonce = (h-1)*M < tx1.nonce = h*M - k`, so the check passes and `tx1` is accepted and executed a second time.

This defeats the "no transaction executes twice" invariant the nonce mechanism is meant to guarantee, and it specifically breaks the anti-collision design documented at `initial_nonce_value` (referencing near/nearcore#3779): that design assumes a freshly-seeded key's floor `(h-1)*M` is always below any nonce a legitimately incrementing wallet would have used, but it does not account for an attacker deliberately choosing a nonce close to the current block's ceiling `h*M` before triggering same-block key deletion/recreation.

### Impact Explanation
This allows a fully unprivileged account owner to force double execution of one of their own previously-executed transactions against a third-party receiver contract (e.g., a `FunctionCall` claim, transfer, or withdrawal). If the receiving contract's logic is not separately idempotent against replayed nonces/signatures (which it normally isn't expected to be, since NEAR's protocol nonce check is supposed to provide that guarantee), this results in double-spend/double-claim of funds — matching the "double-spend/replay" bounty category.

### Likelihood Explanation
Preconditions: attacker needs (a) a `FunctionCall`-restricted key `pk_A` used for the valuable transaction, and (b) any `FullAccess` key on the same account (which any account owner has by default) to perform the `DeleteKey`+`AddKey`. Cost is just two ordinary transactions' gas — no validator or protocol-level access needed, fully attacker-controlled nonce selection, and repeatable for any transaction whose effect is not separately deduplicated by the receiving contract.

### Recommendation
When re-adding an access key with a public key that previously existed on the account, `add_regular_key` (and `add_gas_key`) should not blindly reset the nonce to `initial_nonce_value(block_height)`. Instead, the seed should be the maximum of `initial_nonce_value(block_height)` and any nonce previously recorded for that exact public key (e.g., by tracking a per-account "highest nonce ever seen for this pubkey" independent of key deletion), or nonce seeding should incorporate a monotonically increasing global counter that cannot decrease within/across blocks for a given pubkey.

### Proof of Concept
Integration/runtime-apply test:
1. Set up account `alice.near` with `FunctionCall` key `pk_A` (permission scoped to contract `bob.near`, method `claim`) and `FullAccess` key `pk_B`.
2. At block height `h`, submit `tx1`: signed by `pk_A`, nonce `= h*M - 5`, calling `bob.near::claim()` (a call that increments a counter or transfers tokens once). Assert `tx1` succeeds and `bob.near`'s state reflects one claim.
3. In the same chunk/block `h`, submit `tx2`: signed by `pk_B`, actions `[DeleteKey(pk_A), AddKey(pk_A, same FunctionCall permission)]`, with nonce `h*M - 1` (valid since `> h*M-5` and `< h*M`). Assert it succeeds.
4. Query `pk_A`'s stored access key nonce and assert it equals `(h-1)*M`, which is `< h*M - 5`.
5. In a subsequent block (still within `transaction_validity_period`), resubmit the byte-identical `tx1` (same signature/nonce). Assert `verify_nonce`/`process_tx` accepts it (`ProcessTxResponse::ValidTx`), it executes successfully again, and `bob.near`'s claim counter/balance now reflects two executions instead of one — demonstrating the double-execution/replay.

### Citations

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
