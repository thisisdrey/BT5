### Title
Missing aggregate balance check for pending gas-key transactions when the SPICE pending-transaction-queue is disabled — ([File: chain/client/src/chunk_producer.rs])

### Summary
The reported op-geth bug is that a "sponsor" balance is validated per-transaction, not against the sum of all transactions the sponsor is currently backing, allowing many individually-valid sponsored transactions to be admitted even though only one can actually succeed. Nearcore's "gas key" feature is the closest unprivileged-signer analog: a gas key is a separate balance pool (independent of the account's main balance) that can back multiple concurrently pending transactions identified by distinct `nonce_index` values [1](#0-0) . Nearcore has already identified this exact bug class for its SPICE chunk-production model (which decouples chunk production from execution/certification) and built the `PendingTransactionQueue` / `PendingTxSession` mechanism specifically to aggregate `paid_from_balance` / `paid_from_gas_key` across all currently-produced-but-uncertified chunks [2](#0-1) . However, this protection is gated behind a runtime flag, `spice_pending_transaction_queue_enabled`, and when it is disabled the code falls back to always admitting with zero pending constraints — reproducing the exact per-transaction-only balance check that the bug report describes.

### Finding Description
In the SPICE chunk-preparation path, `prepare_transactions` calls `check_pending` on each candidate transaction to decide whether to admit it and to compute `PendingConstraints` (an aggregate of balance/gas-key balance already committed to other pending, uncertified chunks): [3](#0-2) 

The critical branch is:
```rust
&mut |tx, has_contract| {
    if ptq_enabled {
        session.check_pending(tx, has_contract)
    } else {
        PendingTxCheckResult::Admit(PendingConstraints::default())
    }
},
```
When `ptq_enabled` (`spice_pending_transaction_queue_enabled`) is `false`, every transaction is admitted with `PendingConstraints::default()`, i.e. `paid_from_balance = 0` and `paid_from_gas_key = 0` [4](#0-3) .

Those constraints are fed straight into `verify_and_charge_gas_key_tx_ephemeral`, which validates a gas-key transaction's cost against `gas_key_info.balance - pending.paid_from_gas_key`: [1](#0-0) 

Because SPICE decouples chunk **production** from chunk **certification/execution** (multiple chunks can be produced and queued before any of them is actually executed against real state), several chunk-production rounds can each independently query the *same* certified gas-key balance and each admit a different gas-key transaction (using a different `nonce_index` slot) that the gas key can individually afford — but that combined exceed the key's real balance. This is structurally identical to the reported `validateMetaTxList` bug: the check is done against the account's authoritative balance snapshot, not against the sum of everything already provisionally committed. The `PendingTransactionQueue`/`PendingTxSession` code and its accompanying tests explicitly exist to close this gap ("With a large enough paid_from_balance, subsequent transactions should fail balance validation") [5](#0-4)  — confirming the maintainers consider the un-aggregated check a genuine vulnerability that must be fixed by tracking `paid_from_balance`/`paid_from_gas_key` across the pending window. When the flag is off, that fix is not applied even though the SPICE code path (which requires it) is active.

### Impact Explanation
If SPICE is enabled but `spice_pending_transaction_queue_enabled` is left off (e.g. during rollout, misconfiguration, or a node running mismatched flags relative to the network's actual execution model), an attacker holding a gas key with multiple `num_nonces` slots can submit several gas-key transactions that each pass the (unaggregated) balance check during production of successive uncertified chunks, while the key can only truly fund one of them. When these chunks are eventually certified/executed against real state, the assumption used at production time (that each transaction was individually affordable) no longer holds in aggregate, which can lead to chunks that were optimistically produced becoming invalid at certification — causing state-root divergence between what chunk production assumed and what execution actually produces, and/or breaking the SPICE certification pipeline for the shard (chunk-processing/halt condition), analogous to the DoS/txpool-poisoning effect described in the original report.

### Likelihood Explanation
This requires SPICE to be active with `spice_pending_transaction_queue_enabled = false`, and an unprivileged signer to own or control a gas key with more than one nonce slot (`num_nonces > 1`), which is a normal, permissionless account configuration. No validator or node compromise is needed — only the flag misconfiguration/rollout gap. Because the flag exists precisely to toggle this fix on/off, it is plausible for some subset of production configurations (particularly during a phased rollout of SPICE and the pending-transaction-queue feature) to run with the flag disabled while SPICE's chunk/execution decoupling is already active, exposing the window.

### Recommendation
Do not allow `spice_pending_transaction_queue_enabled = false` while `ProtocolFeature::Spice` is active, or otherwise ensure that whenever chunk production and certification are decoupled, `PendingTxSession::check_pending` (and its `paid_from_balance` / `paid_from_gas_key` aggregation) is always exercised rather than defaulting to `PendingConstraints::default()`. Alternatively, tie the pending-transaction-queue enforcement directly to the Spice protocol feature flag itself rather than to a separate, independently toggleable config value.

### Proof of Concept
Not directly reproducible without a live SPICE-enabled testnet with the flag disabled; the vulnerable code path and its intended fix are directly visible in the cited source:
- Vulnerable fallback: [6](#0-5) 
- Per-tx-only check that becomes exploitable when constraints are zeroed: [1](#0-0) 
- Existing regression test proving the aggregation is what prevents the overspend (i.e., without it, the described bug reproduces): [5](#0-4)

### Citations

**File:** runtime/runtime/src/verifier.rs (L420-440)
```rust

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
```

**File:** chain/client/src/pending_transaction_queue.rs (L427-446)
```rust
/// Per-chunk-production session. Combines pending transaction queue state with session-local tracking
/// for constraints NOT handled by the ephemeral TrieUpdate.
///
/// The ephemeral TrieUpdate handles within-chunk accumulation for balance
/// (deducts cost), gas key balance (deducts gas_key_cost), and nonces
/// (advances after each accepted tx). The session only tracks what the
/// ephemeral state does NOT cover:
/// - P_MAX / deploy exclusivity counts (per account)
/// - WithdrawFromGasKey amounts (action effects not applied by ephemeral state)
///
/// The session holds an `Arc<Mutex<ShardedPendingTransactionQueue>>` and acquires the lock briefly
/// per transaction rather than holding it for the entire chunk production duration. This avoids
/// blocking block processing and RPC handlers.
pub struct PendingTxSession {
    pending_transaction_queue: Arc<Mutex<ShardedPendingTransactionQueue>>,
    shard_uid: ShardUId,
    session_access_key_tx_counts: HashMap<AccountId, usize>,
    session_deploy_tx_counts: HashMap<AccountId, usize>,
    session_gas_key_withdrawals: HashMap<(AccountId, PublicKey), Balance>,
}
```

**File:** chain/client/src/chunk_producer.rs (L530-551)
```rust
                let mut session =
                    PendingTxSession::new(Arc::clone(&self.pending_transaction_queue), shard_uid);
                let ptq_enabled = self.spice_pending_transaction_queue_enabled;
                let (prepared, skipped) = self.runtime_adapter.prepare_transactions_extra(
                    state_update,
                    shard_id,
                    prev_block_context,
                    &mut iter,
                    chain_validate,
                    validate_tx_ttl,
                    std::collections::HashSet::new(),
                    &mut |tx, has_contract| {
                        if ptq_enabled {
                            session.check_pending(tx, has_contract)
                        } else {
                            PendingTxCheckResult::Admit(PendingConstraints::default())
                        }
                    },
                    self.chunk_transactions_time_limit.get(),
                    None,
                )?;
                (prepared, skipped.0)
```

**File:** chain/chain/src/types.rs (L453-468)
```rust
/// Result of checking pending transaction queue admission for a transaction.
#[derive(Debug, PartialEq, Eq)]
pub enum PendingTxCheckResult {
    /// Admitted. Use these constraints for balance/nonce validation.
    Admit(PendingConstraints),
    /// Violates PTQ constraints (P_MAX, deploy exclusivity).
    /// Push to skipped_transactions for reintroduction to pool.
    Skip,
}

impl PendingTxCheckResult {
    /// Returns a closure that always admits with default constraints.
    pub fn always_admit() -> impl FnMut(&SignedTransaction, HasContract) -> PendingTxCheckResult {
        |_, _| PendingTxCheckResult::Admit(PendingConstraints::default())
    }
}
```

**File:** chain/chain/src/runtime/tests.rs (L2175-2227)
```rust
/// When check_pending returns Admit with paid_from_balance, the available
/// balance for validation is reduced. With a large enough paid_from_balance,
/// subsequent transactions should fail balance validation.
#[test]
fn test_prepare_transactions_pending_balance_constraint() {
    let (env, chain, _) = get_test_env_with_chain_and_pool();

    // Create a pool with a small transfer from test1.
    let signer = InMemorySigner::test_signer(&"test1".parse::<AccountId>().unwrap());
    let tx = SignedTransaction::send_money(
        1,
        signer.get_account_id(),
        "test2".parse().unwrap(),
        &signer,
        Balance::from_yoctonear(1),
        env.head.prev_block_hash,
    );
    let mut pool = TransactionPool::new(TEST_SEED, None, "");
    pool.insert_transaction(ValidatedTransaction::new_for_test(tx));

    // Without pending constraints, the transfer should succeed.
    let (prepared, _) = prepare_transactions_extra(
        &env,
        &chain,
        &mut PoolIteratorWrapper::new(&mut pool),
        HashSet::new(),
        &|_| true,
        &mut PendingTxCheckResult::always_admit(),
        None,
    )
    .unwrap();
    assert_eq!(prepared.transactions.len(), 1);

    // Reinsert and try again with paid_from_balance consuming all balance.
    pool.insert_transaction(prepared.transactions[0].clone());
    let (prepared, _) = prepare_transactions_extra(
        &env,
        &chain,
        &mut PoolIteratorWrapper::new(&mut pool),
        HashSet::new(),
        &|_| true,
        &mut |_, _| {
            PendingTxCheckResult::Admit(PendingConstraints {
                paid_from_balance: TESTING_INIT_BALANCE,
                ..PendingConstraints::default()
            })
        },
        None,
    )
    .unwrap();
    // The transaction should fail balance validation (balance reduced to 0).
    assert_eq!(prepared.transactions.len(), 0);
}
```
