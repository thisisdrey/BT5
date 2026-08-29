### Title
NEAR-implicit account nonce reseed can regress below an already-consumed nonce when delete-and-recreate happen within the same block, enabling transaction replay - (`runtime/runtime/src/actions.rs::action_implicit_account_creation_transfer`)

### Summary
`action_implicit_account_creation_transfer` reseeds a recreated NEAR-implicit account's access-key nonce to `initial_nonce_value(block_height) = (block_height-1) * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` using the *current* block height [1](#0-0) . This is safe when a delete-then-recreate spans multiple blocks (the intended, tested case), but because transaction nonce consumption happens in the tx-processing phase (before receipts execute) while `DeleteAccount` only actually removes the account when its *local receipt* executes later in the same block's receipt-processing phase, an account can be deleted and re-funded (via a pending incoming `Transfer`) within one and the same block. The reseeded nonce for that block can then be lower than a nonce already consumed earlier in that same block's transaction-processing phase, allowing the earlier (already-executed) signed transaction to be validly resubmitted and re-applied.

### Finding Description
`action_implicit_account_creation_transfer` sets `access_key.nonce = initial_nonce_value(block_height)` purely as a function of the block height at which the *receipt* recreating the account executes [2](#0-1) . `verify_nonce` enforces `tx_nonce <= current_nonce` fails and an upper bound `tx_nonce < block_height * ACCESS_KEY_NONCE_RANGE_MULTIPLIER` [3](#0-2) . Both nonce consumption (`verify_and_charge_tx_ephemeral`) and the eventual reseed use the *same* block height when the two events happen to occur in the same block, because:

- Transaction verification/nonce-consumption happens in `process_transactions` (Phase 7), which mutates `access_key.nonce` in the in-memory `TrieUpdate` immediately, before any receipts execute [4](#0-3) , [5](#0-4) .
- `DeleteAccount` only actually removes the account/access-key when its *receipt* is executed, which happens later, in Phase 8 (`process_receipts`), and specifically in the **local receipts** sub-phase, which runs strictly before **incoming receipts** [6](#0-5) .
- A `DeleteAccount` transaction where `signer_id == receiver_id` (the implicit account deleting itself) is converted into a *local receipt* and thus executes in the same block as the transaction that spawned it [7](#0-6) , while a `Transfer` from a distinct funder account to that implicit account is *not* local (signer != receiver) and is only delivered as an *incoming* receipt in a later processing step — but that can still land in the same block if it was generated the previous block.

Exploit flow (unprivileged attacker controls the implicit account's private key, since it is derived deterministically from the account_id itself):
1. Implicit account `IA` already exists (created earlier) with a full-access key.
2. At block `M-1`, attacker (or anyone) submits a `Transfer` from a distinct funded account `F` to `IA`. Since `F != IA`, this becomes an outgoing/incoming receipt scheduled to arrive at block `M`.
3. At block `M`, attacker submits, signed by `IA`: tx_A with nonce `N` (e.g., a `Transfer` moving `IA`'s balance elsewhere) and tx_B with nonce `N+1` (`DeleteAccount`, self-targeting). Both are validated/charged in Phase 7 using block `M`'s upper bound (`N < M * 1_000_000`), advancing `access_key.nonce` and creating tx_B's `DeleteAccount` receipt as a *local* receipt.
4. In Phase 8 of block `M`: local receipts run first — tx_B's `DeleteAccount` receipt executes, wiping `IA`'s account and access key. Then incoming receipts run — the `Transfer` from step 2 finds `IA` absent and calls `action_implicit_account_creation_transfer` with `block_height = M`, reseeding the nonce to `(M-1) * 1_000_000`.
5. Because `N` was chosen (validly) close to the top of block `M`'s allowed window, `N > (M-1)*1_000_000` in the overwhelmingly common case, so the already-executed, already-recorded tx_A remains "valid" against the freshly reseeded key.
6. Attacker resubmits the identical signed tx_A (same bytes/hash) in a subsequent block within its `transaction_validity_period` window. `verify_nonce` accepts it (`N > current_nonce`), and there is no other duplicate-hash defense outside a chunk — `UniqueChunkTransactions` only dedupes within a single chunk [8](#0-7) . tx_A's action (e.g., the `Transfer`) executes a second time.

This differs from, and is not covered by, the existing regression test `test_transaction_hash_collision_for_near_implicit_account_fail`, which explicitly separates creation, deletion, and recreation across multiple blocks (`blocks_number = 5` between each step) [9](#0-8) , confirming the intended cross-block protection works, but leaving the same-block interleaving path (Phase 7 nonce consumption vs. Phase 8 local-then-incoming receipt execution) untested and unguarded.

### Impact Explanation
If exploited, this allows replay of an already-executed, previously-successful transaction signed by a NEAR-implicit account after its underlying account is deleted and recreated within a single block. Depending on the replayed action, this can duplicate a `Transfer` (double-spend / value duplication, breaking `VALUE_CONSERVATION`), or replay any other full-access action signed with that key. This maps to NEAR bounty categories of double-spend/replay and loss/duplication of funds.

### Likelihood Explanation
Exploitation requires no privileged role — only an ordinary account funding/refunding the implicit account and the implicit account's own key (always available to whoever generated it). The hard part is achieving the specific intra-block interleaving (recreate-`Transfer` incoming receipt and self `DeleteAccount` local receipt landing in the same block, with a favorably-large stale nonce), which depends on transaction/receipt scheduling that an unprivileged submitter does not fully control in production (chunk producer decides final block/receipt placement), making it probabilistic rather than deterministic on live mainnet. It is, however, fully deterministic and reproducible in a `runtime/runtime/src/tests/apply.rs`-style unit test or an integration test-loop harness that directly drives `Runtime::apply` with a crafted `ApplyState`/transaction/receipt set for a single block, as `apply.rs` and the existing `access_key_nonce_for_implicit_accounts.rs` file demonstrate is a common test pattern for this exact code path.

### Recommendation
Do not derive the reseeded nonce purely from the current block height when recreating a NEAR-implicit account. Instead, ensure the new baseline nonce is provably higher than any nonce that could have been consumed by the account's prior incarnation in the *same* block — e.g., track and persist the maximum nonce ever used by the account (even across deletion) and reseed to `max(initial_nonce_value(block_height), last_known_nonce + 1)`, or ensure `DeleteAccount` receipts are always ordered strictly after any incoming receipts of the same block that could recreate the same account id, or reseed using the block height at which the *transaction* that ultimately consumed the highest nonce was processed rather than the block height of receipt execution alone.

### Proof of Concept
Runtime unit test plan (in `runtime/runtime/src/tests/apply.rs` style, single `Runtime::apply` call for block `M`):
1. Pre-seed state (via a prior `apply` at block `M-1`) with implicit account `IA` already existing with access key nonce base `B0 = (M-2)*1_000_000`.
2. Submit an outgoing `Transfer` from funder `F` to `IA` at block `M-1` so its receipt is scheduled as incoming for block `M`.
3. At block `M`, submit `tx_A` = `Transfer` (or any full-access action) signed by `IA` with nonce `N` chosen such that `B0 < N < M*1_000_000` and preferably `N > (M-1)*1_000_000`, followed by `tx_B` = `DeleteAccount(signer=IA, receiver=IA, beneficiary=F)` with nonce `N+1`.
4. Call `Runtime::apply` once for block `M` with these two transactions plus the previously-scheduled incoming `Transfer` receipt.
5. Assert: `IA` exists post-apply with `access_key.nonce == (M-1)*1_000_000` (or otherwise `< N`).
6. Resubmit the exact same signed `tx_A` bytes at block `M+1` (within `transaction_validity_period`) via `Runtime::apply` or `validate_verify_and_charge_transaction`, and assert it is **wrongly accepted** (`TxVerdict::Success` / `ProcessTxResponse::ValidTx`) instead of rejected with `InvalidTxError::InvalidNonce`, demonstrating the replay of `tx_A`'s action a second time.

### Citations

**File:** runtime/runtime/src/actions.rs (L213-231)
```rust
pub(crate) fn action_implicit_account_creation_transfer(
    state_update: &mut TrieUpdate,
    apply_state: &ApplyState,
    fee_config: &RuntimeFeesConfig,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    account_id: &AccountId,
    deposit: Balance,
    block_height: BlockHeight,
    epoch_info_provider: &dyn EpochInfoProvider,
) {
    *actor_id = account_id.clone();
    match account_id.get_account_type() {
        AccountType::NearImplicitAccount => {
            let mut access_key = AccessKey::full_access();
            access_key.nonce = initial_nonce_value(block_height);

            // unwrap: here it's safe because the `account_id` has already been determined to be implicit by `get_account_type`
            let public_key = PublicKey::from_near_implicit_account(account_id).unwrap();
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

**File:** runtime/runtime/src/lib.rs (L2110-2153)
```rust
            // Verify and charge based on transaction type (gas key vs regular access key)
            let verdict = if let Some(nonce_index) = tx.transaction.nonce().nonce_index() {
                // Gas key transaction - load nonce from prefetched cache
                let nonce_entry = gas_key_nonces.get(&(signer_id, pubkey, nonce_index));
                let current_nonce = match nonce_entry.as_deref() {
                    Some(Ok(Some(n))) => *n,
                    Some(Ok(None)) => {
                        metrics::TRANSACTION_PROCESSED_FAILED_TOTAL.inc();
                        tracing::debug!(%tx_hash, "gas key nonce not found");
                        let num_nonces =
                            access_key.gas_key_info().map(|info| info.num_nonces).unwrap_or(0);
                        let outcome = ExecutionOutcomeWithId::failed(
                            tx,
                            InvalidTxError::InvalidNonceIndex {
                                tx_nonce_index: Some(nonce_index),
                                num_nonces,
                            },
                        );
                        processing_state.outcomes.push(outcome);
                        continue;
                    }
                    Some(Err(e)) => return Err(e.clone().into()),
                    None => unreachable!("gas key nonces should've been prefetched"),
                };
                verify_and_charge_gas_key_tx_ephemeral(
                    &processing_state.apply_state.config,
                    account,
                    access_key,
                    current_nonce,
                    &tx.transaction,
                    &cost,
                    Some(block_height),
                    &PendingConstraints::default(),
                )
            } else {
                // Regular access key transaction
                verify_and_charge_tx_ephemeral(
                    &processing_state.apply_state.config,
                    account,
                    access_key,
                    &tx.transaction,
                    &cost,
                    Some(block_height),
                    &PendingConstraints::default(),
```

**File:** runtime/runtime/src/lib.rs (L2216-2224)
```rust
                    if receipt.receiver_id() == signer_id {
                        processing_state.local_receipts.push_back(receipt);
                    } else {
                        receipt_sink.forward_or_buffer_receipt(
                            receipt,
                            &processing_state.apply_state,
                            &mut processing_state.state_update,
                        )?;
                    }
```

**File:** runtime/runtime/src/lib.rs (L2259-2273)
```rust
            let compute = outcome
                .outcome
                .compute_usage
                .expect("`process_transaction` must populate compute usage");
            processing_state.total.add(outcome.outcome.gas_burnt.as_gas(), compute)?;
            processing_state.outcomes.push(outcome);

            result.apply(account, access_key);
            set_account(&mut processing_state.state_update, signer_id.clone(), account);
            // Update gas key nonce if applicable
            if let Some((nonce_index, new_nonce)) = result.gas_key_nonce_update() {
                set_gas_key_nonce(
                    &mut processing_state.state_update,
                    signer_id.clone(),
                    pubkey.clone(),
```

**File:** protocol-model/spec/runtime-execution.md (L27-39)
```markdown
`Runtime::apply` (`runtime/runtime/src/lib.rs:1717`) runs conceptually as: (1) validator accounts, (2) transactions, (3) receipts, (4) finalize. Concretely, in order:

1. **Prefetch** transaction data (best-effort) (`runtime/runtime/src/lib.rs:1746`).
2. **Update validator accounts** if a `ValidatorAccountsUpdate` is supplied (`runtime/runtime/src/lib.rs:1752` → `update_validator_accounts` `:1599`). This happens first so reward/stake changes land before any tx/receipt reads balances, and it commits with `StateChangeCause::ValidatorAccountsUpdate` (`runtime/runtime/src/lib.rs:1692`).
3. **Load the delayed receipt queue** (`runtime/runtime/src/lib.rs:1759`).
4. **Run the bandwidth scheduler** — for every chunk including missing ones (`runtime/runtime/src/lib.rs:1767` — `run_bandwidth_scheduler`).
5. **Missing-chunk short-circuit**: if `!apply_state.is_new_chunk`, finalize immediately and return without processing any receipts (`runtime/runtime/src/lib.rs:1775` → `missing_chunk_apply_result` `:2937`), carrying congestion info and bandwidth requests forward unchanged.
6. **Build the `ReceiptSink`** from own congestion info + scheduler output, then **forward buffered receipts** from prior chunks first (`runtime/runtime/src/lib.rs:1787`, `:1795`). See [cross-shard congestion](cross-shard-congestion.md).
7. **Process transactions** → local receipts / forwarded receipts (`runtime/runtime/src/lib.rs:1798` — `process_transactions`).
8. **Process receipts** in the order local → delayed → incoming, then resolve promise-yield timeouts (`runtime/runtime/src/lib.rs:1801` — `process_receipts` `:2658`).
9. **Finalize**: `validate_apply_state_update` (`runtime/runtime/src/lib.rs:1813` / `:2723`) — persist promise-yield indices, finalize congestion (choose allowed shard), generate bandwidth requests, apply the sandbox state patch, run `state_update.finalize()`, dedup validator proposals (keeping the last per account, reversed) (`runtime/runtime/src/lib.rs:2816`), and assemble `ApplyResult`.

The local → delayed → incoming order matters: local receipts (from this chunk's transactions) and the delayed backlog are drained before new incoming cross-shard receipts, so backlog is preferred over new work and gas-limit exhaustion pushes the newest work to the delayed queue (`runtime/runtime/src/lib.rs:2670`-`2692`).
```

**File:** core/primitives-core/src/version.rs (L360-365)
```rust
    /// Skip transactions whose hash already appeared earlier in the same chunk.
    /// A transaction hash is also its outcome id, and outcomes are committed
    /// (via the chunk outcome root) keyed by that id. Including a transaction
    /// twice would otherwise commit two conflicting outcomes (a success and an
    /// InvalidNonce failure) under one id.
    UniqueChunkTransactions,
```

**File:** integration-tests/src/tests/features/access_key_nonce_for_implicit_accounts.rs (L112-211)
```rust
/// Helper for checking that duplicate transactions from NEAR-implicit accounts are properly rejected.
/// It creates NEAR-implicit account, deletes it and creates again, so that nonce of the access
/// key is updated. Then it tries to send tx from NEAR-implicit account with invalid nonce, which
/// should fail since the protocol upgrade.
fn get_status_of_tx_hash_collision_for_near_implicit_account(
    protocol_version: ProtocolVersion,
    near_implicit_account_signer: Signer,
) -> ProcessTxResponse {
    let epoch_length = 100;
    let mut genesis = Genesis::test(vec!["test0".parse().unwrap(), "test1".parse().unwrap()], 1);
    genesis.config.epoch_length = epoch_length;
    genesis.config.transaction_validity_period = epoch_length * 2;
    genesis.config.protocol_version = protocol_version;
    let mut env = TestEnv::builder(&genesis.config).nightshade_runtimes(&genesis).build();
    let genesis_block = env.clients[0].chain.get_block_by_height(0).unwrap();
    let deposit_for_account_creation = Balance::from_millinear(100);
    let mut height = 1;
    let blocks_number = 5;
    let signer1 = InMemorySigner::test_signer(&"test1".parse().unwrap());
    let near_implicit_account_id = near_implicit_account_signer.get_account_id();

    // Send money to NEAR-implicit account, invoking its creation.
    let send_money_tx = SignedTransaction::send_money(
        1,
        "test1".parse().unwrap(),
        near_implicit_account_id.clone(),
        &signer1,
        deposit_for_account_creation,
        *genesis_block.hash(),
    );
    height = check_tx_processing(&mut env, send_money_tx, height, blocks_number);
    let block = env.clients[0].chain.get_block_by_height(height - 1).unwrap();

    // Delete NEAR-implicit account.
    let delete_account_tx = SignedTransaction::delete_account(
        // Because AccessKeyNonceRange is enabled, correctness of this nonce is guaranteed.
        (height - 1) * near_primitives::account::AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER,
        near_implicit_account_id.clone(),
        near_implicit_account_id.clone(),
        "test0".parse().unwrap(),
        &near_implicit_account_signer,
        *block.hash(),
    );
    height = check_tx_processing(&mut env, delete_account_tx, height, blocks_number);
    let block = env.clients[0].chain.get_block_by_height(height - 1).unwrap();

    // Send money to NEAR-implicit account again, invoking its second creation.
    let send_money_again_tx = SignedTransaction::send_money(
        2,
        "test1".parse().unwrap(),
        near_implicit_account_id.clone(),
        &signer1,
        deposit_for_account_creation,
        *block.hash(),
    );
    height = check_tx_processing(&mut env, send_money_again_tx, height, blocks_number);
    let block = env.clients[0].chain.get_block_by_height(height - 1).unwrap();

    // Send money from NEAR-implicit account with incorrect nonce.
    let send_money_from_near_implicit_account_tx = SignedTransaction::send_money(
        1,
        near_implicit_account_id.clone(),
        "test0".parse().unwrap(),
        &near_implicit_account_signer,
        Balance::from_yoctonear(100),
        *block.hash(),
    );
    let response =
        env.rpc_handlers[0].process_tx(send_money_from_near_implicit_account_tx, false, false);

    // Check that sending money from NEAR-implicit account with correct nonce is still valid.
    let send_money_from_near_implicit_account_tx = SignedTransaction::send_money(
        (height - 1) * AccessKey::ACCESS_KEY_NONCE_RANGE_MULTIPLIER,
        near_implicit_account_id,
        "test0".parse().unwrap(),
        &near_implicit_account_signer,
        Balance::from_yoctonear(100),
        *block.hash(),
    );
    check_tx_processing(&mut env, send_money_from_near_implicit_account_tx, height, blocks_number);

    response
}

/// Test that duplicate transactions from NEAR-implicit accounts are properly rejected.
#[test]
fn test_transaction_hash_collision_for_near_implicit_account_fail() {
    let secret_key = SecretKey::from_seed(KeyType::ED25519, "test");
    let public_key = secret_key.public_key();
    let near_implicit_account_id = derive_near_implicit_account_id(public_key.unwrap_as_ed25519());
    let near_implicit_account_signer =
        InMemorySigner::from_secret_key(near_implicit_account_id, secret_key);
    assert_matches!(
        get_status_of_tx_hash_collision_for_near_implicit_account(
            PROTOCOL_VERSION,
            near_implicit_account_signer
        ),
        ProcessTxResponse::InvalidTx(InvalidTxError::InvalidNonce { .. })
    );
}
```
