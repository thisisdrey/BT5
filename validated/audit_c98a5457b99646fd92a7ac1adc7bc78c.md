### Title
Same-height delete+recreate of a NEAR-implicit account collapses access-key nonce seeding, enabling tx replay/double-spend - ([File: runtime/runtime/src/access_keys.rs])

### Finding Description
`initial_nonce_value` seeds a freshly (re)created regular access key's nonce to `(block_height - 1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`, both for `add_regular_key` (explicit `AddKey`) and for NEAR-implicit account (re)creation via `action_implicit_account_creation_transfer` → `add_regular_key`. [1](#0-0) [2](#0-1)  This seed was introduced specifically to prevent tx-hash/nonce collisions across account re-creations (issue #3779), and its correctness implicitly depends on `block_height` strictly increasing between the moment a key's nonce was last advanced and the moment the key is re-seeded after deletion.

`verify_nonce` enforces `tx_nonce > current_nonce` (Monotonic) and `tx_nonce < block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` (upper bound), where `block_height` is the height at verification time of *that* transaction. [3](#0-2) 

`Runtime::apply` processes, within one chunk-apply call (a single fixed `block_height` H): (1) all transactions in the chunk (nonce checks/advances happen here, for every tx regardless of whether its resulting receipt is local or forwarded), then (2) receipts in strict order **local → delayed → incoming**. [4](#0-3)  A self-signed `DeleteAccount` transaction (signer_id == receiver_id == the implicit account) produces a **local** receipt, executed before any **incoming** receipt in the same chunk.

Exploit flow, all confined to a single block height H:
1. Attacker's NEAR-implicit account already exists (created earlier) and has funds.
2. Attacker submits, in the same chunk at height H, two self-signed transactions from the implicit account with sequential nonces: (a) nonce `N` (chosen close to but below `H * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`) that transfers funds out (e.g., `SendMoney`), and (b) nonce `N+1`, a `DeleteAccount` action (self-signer=self-receiver, so it becomes a **local** receipt).
3. Attacker also arranges (via an account they control) a `Transfer` receipt targeting the same implicit account id to arrive as an **incoming** receipt at the very same height H — recreating the account.
4. In phase 2 of `apply` (transactions), the access key nonce is advanced to `N` when tx (a) executes; tx (b) is validated/charged at nonce `N+1`.
5. In phase 3, the local `DeleteAccount` receipt runs first, removing the account and its access key. The incoming `Transfer` receipt then runs, recreating the account and re-seeding `access_key.nonce = (H-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` — a value strictly less than `N` (since `N` was chosen just below `H * ACCESS_KEY_NONCE_RANGE_MULTIPLIER`).
6. At a later block `H2 > H` (within the tx's validity window), the attacker resubmits the *exact same signed bytes* of tx (a) (nonce `N`). `verify_nonce` checks `N > current_nonce = (H-1)*MULT` (true) and `N < H2*MULT` (true, trivially) — the replayed transaction is accepted and re-executed, moving the same funds a second time.

The `#3779` mitigation assumes that any re-creation of a key happens at a strictly later block height than the height at which the tx that consumed the previous incarnation's nonce was verified. That assumption is violated here because the delete and the recreate can be forced into the *same* `apply()` call as the original high-nonce transaction, due to the fixed local-before-incoming receipt ordering within one chunk.

### Impact Explanation
This allows an unprivileged attacker to replay an already-executed transaction against a recreated NEAR-implicit account, causing a double-spend of the account's balance (e.g., double execution of a `Transfer`/`SendMoney` action), i.e., token loss/duplication tied to a single funding event — directly matching the "double-spend/replay" bounty category.

### Likelihood Explanation
Preconditions require an attacker to fully control: the implicit account being attacked (its private key), a funding/recreation source account, and precise timing so that (a) a self-signed high-nonce tx and a self-signed `DeleteAccount` tx land in the same chunk, and (b) a `Transfer` receipt recreating the account arrives as an *incoming* receipt in that same chunk/height. This timing is not fully attacker-controlled (chunk composition/receipt delivery latency depends on the network), but the attacker can retry the sequence across many blocks at negligible cost (only gas/storage fees) until the alignment occurs, making this a repeatable, low-cost attack rather than a one-shot opportunity.

### Recommendation
Re-seed the nonce using a value that also accounts for the previous nonce, e.g. `new_nonce = max((block_height-1)*MULT, previous_nonce + 1)` (persisting the last-known nonce across deletion), or use a monotonically increasing, block-independent counter (e.g. a running "deletion generation" stored per account id) so re-creation within the same block height as a prior high nonce cannot roll the nonce backward below an already-used value.

### Proof of Concept
Integration/runtime test (extending `access_key_nonce_for_implicit_accounts.rs`):
1. Create NEAR-implicit account (Transfer from `test1`), let it settle.
2. Within a single produced block at height H, include as a chunk: (a) self-signed `SendMoney` tx from the implicit account with nonce `N = H*ACCESS_KEY_NONCE_RANGE_MULTIPLIER - 1` transferring balance to `test0`, (b) self-signed `DeleteAccount` tx with nonce `N+1`, and ensure a `Transfer` from `test1` to the same implicit account id is delivered as an incoming receipt at height H (submit it from `test1` one block earlier, targeting a different shard, or use single-shard local test setup with controlled receipt delay).
3. Confirm balance after block H reflects account recreated with fresh access key nonce `(H-1)*MULT`.
4. At height `H2 > H` (within `transaction_validity_period`), resubmit the identical signed bytes of tx (a).
5. Assert `ProcessTxResponse::ValidTx` and a second successful execution transferring funds again — i.e., assert the current behavior incorrectly allows replay instead of `InvalidTxError::InvalidNonce`/`NonceTooLarge`.

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

**File:** runtime/runtime/src/verifier.rs (L211-236)
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
```

**File:** runtime/runtime/src/lib.rs (L2693-2715)
```rust
        // We first process local receipts. They contain staking, local contract calls, etc.
        self.process_local_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;

        // Then we process the delayed receipts. It's a backlog of receipts from the past blocks.
        self.process_delayed_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;

        // And then we process the new incoming receipts. These are receipts from other shards.
        self.process_incoming_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;
```
