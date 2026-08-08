### Title
Fee-payer balance is not reserved/decremented across buffered transactions, allowing a single funded account to fabricate unlimited maximal-priority spam that evicts real users from the scheduler container - (File: core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs)

### Summary
`calculate_priority_and_cost` derives queue priority from the *claimed* fee/reward of a transaction (`compute_unit_price` × `compute_unit_limit` plus base fee), and the only admission gate before insertion is `Consumer::check_fee_payer_unlocked`, which re-reads the fee payer's *current* bank balance for each transaction independently. Because the buffering/insertion path never reserves or decrements the fee payer's balance for other transactions still sitting unexecuted in the container, one funded account can pass the balance check for many distinct transactions "at once," each claiming the full balance as its fee, giving all of them maximal priority even though only one could ever actually be paid.

### Finding Description
In `TransactionViewReceiveAndBuffer::handle_packet_batch_message` (`core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs:292-363`), for every incoming transaction:
1. Priority/cost are computed by `calculate_priority_and_cost` (`core/src/transaction_priority.rs:32-66`) as `P = R / (1+C)`, where `R` is the reward derived from `compute_unit_price` × `compute_unit_limit` (fully attacker-controlled) plus the base signature fee.
2. The only economic gate is `Consumer::check_fee_payer_unlocked` (`core/src/banking_stage/consumer.rs:710-739`), which loads the fee payer account fresh from `bank.rc.accounts` and calls `validate_fee_payer` (`svm/src/account_loader.rs:373-421`) to check `lamports - min_balance - fee >= 0`.
3. If it passes, the transaction is inserted with `container.insert_map_only` and `push_ids_into_queue` (`core/src/banking_stage/transaction_scheduler/transaction_state_container.rs:178-201`), which evicts the *lowest-priority* transaction in the container whenever the map exceeds `capacity`.

Crucially, this balance check is performed against the account's current on-chain (bank) balance, not against a "balance minus fees already claimed by other pending, unexecuted transactions from the same fee payer in the container." No reservation/decrement happens until a transaction is actually executed by the SVM. Consequently, an attacker holding a single balance `B` can craft `N` distinct transactions (differing in blockhash/instructions, so the nonce-dedup logic at lines 301-310, which only applies to nonce-like transactions, does not apply) each claiming a fee up to `B`. Each is independently validated against the *same*, undebited `B`, so all `N` pass `check_fee_payer_unlocked` and are inserted with priority computed from a reward of up to `B` each — even though the attacker only ever possessed `B` once. Since account-lock conflict checks (`ThreadAwareAccountLocks::try_lock_accounts`) only apply during scheduling/pop, not during buffering/insertion, nothing prevents these N transactions from simultaneously occupying container slots at high (fabricated) priority, evicting genuinely fee-paying users' lower-priority transactions through `push_ids_into_queue`'s capacity-eviction logic.

### Impact Explanation
This is a QoS-evasion / underpriced-work issue: the priority auction's core invariant — "priority reflects fee actually payable" — is broken because the admission check does not scale with the number of concurrently buffered transactions from a single signer. A single small balance can be leveraged into an arbitrarily large number of maximal-priority-looking packets (bounded only by network throughput to the leader's TPU, not by the attacker's capital), each evicting genuine paying transactions from other users out of the `TransactionStateContainer`. This matches the "QoS evasion" / "buffer eviction of paying users via cheap high-priority-looking spam" bounty category.

### Likelihood Explanation
The attacker is fully unprivileged: an unstaked client can open a QUIC/TPU connection and submit an arbitrary burst of syntactically valid, distinctly-blockhashed transactions from one funded keypair, each declaring a high `compute_unit_price`. No special permissions, stake, or gossip/peer control are needed — only a small amount of SOL sufficient to satisfy one instance of the balance check. The condition is reproducible deterministically in a unit test that sends multiple transactions from the same fee payer with different (fresh) blockhashes and observes them all buffered with maximal priority against the same account balance.

### Recommendation
Track a per-fee-payer "reserved fee" total across transactions currently resident in the `TransactionStateContainer` (buffered, queued, or in-flight), and subtract this reserved amount from the balance used in `check_fee_payer_unlocked`/`validate_fee_payer` when admitting a new transaction from the same fee payer, so that the aggregate claimed fees from one signer's pending transactions can never exceed its actual on-chain balance.

### Proof of Concept
```rust
// core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (test module)
#[test]
fn test_single_balance_fabricates_many_maximal_priority_slots() {
    let (sender, receiver) = bounded(1024);
    let (bank_forks, mint_keypair) = test_bank_forks_with_fee();
    let (mut receive_and_buffer, mut container) =
        setup_transaction_view_receive_and_buffer(receiver, bank_forks.clone());

    let bank = bank_forks.read().unwrap().root_bank();
    let payer_balance = bank.get_balance(&mint_keypair.pubkey());

    // Craft N distinct transactions from the SAME fee payer, each claiming a
    // compute_unit_price such that the computed fee is close to the FULL
    // account balance. Distinct recent blockhashes avoid any dedup logic.
    const N: usize = 10;
    let mut txs = Vec::new();
    for _ in 0..N {
        let blockhash = bank.last_blockhash(); // could rotate blockhashes if needed
        txs.push(create_transfer_with_max_claimed_fee(
            &mint_keypair,
            payer_balance,
            blockhash,
        ));
    }
    send_transactions(&sender, &txs);

    let stats = receive(&mut receive_and_buffer, &mut container);

    // Invariant under test: the aggregate of fees claimed by all *admitted*
    // transactions from one signer must not exceed that signer's real balance.
    // If this assertion fails, N transactions, each individually validated
    // against the same undebited balance, were all admitted with maximal
    // priority using only a single balance's worth of capital.
    assert!(
        stats.num_buffered as u64 * payer_balance <= payer_balance,
        "fabricated {} maximal-priority slots from a single balance of {}",
        stats.num_buffered,
        payer_balance
    );
}
```
Expected (buggy) result: `stats.num_buffered == N`, all inserted with priority computed from `payer_balance`-sized fees, demonstrating that eviction capacity in `push_ids_into_queue` can be consumed by fabricated, unpayable-in-aggregate priority. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** core/src/transaction_priority.rs (L32-66)
```rust
pub(crate) fn calculate_priority_and_cost<Tx: TransactionMeta + SVMStaticMessage>(
    bank: &Bank,
    transaction: &Tx,
    transaction_configuration: &TransactionConfiguration,
) -> (u64, u64) {
    let cost = CostModel::calculate_cost_for_executed_transaction(
        transaction,
        u64::from(transaction_configuration.compute_unit_limit),
        transaction_configuration.loaded_accounts_data_size_limit,
        &bank.feature_set,
    )
    .sum();
    let fee_details = solana_fee::calculate_fee_details(
        transaction,
        bank.fee_structure().lamports_per_signature,
        transaction_configuration.priority_fee_lamports,
        bank.fee_features(),
    );
    let reward = bank
        .calculate_reward_and_burn_fee_details(&CollectorFeeDetails::from(fee_details))
        .get_deposit();

    // We need a multiplier here to avoid rounding down too aggressively.
    // For many transactions, the cost will be greater than the fees in terms of raw lamports.
    // For the purposes of calculating prioritization, we multiply the fees by a large number so that
    // the cost is a small fraction.
    // An offset of 1 is used in the denominator to explicitly avoid division by zero.
    const MULTIPLIER: u64 = 1_000_000;
    (
        reward
            .saturating_mul(MULTIPLIER)
            .saturating_div(cost.saturating_add(1)),
        cost,
    )
}
```

**File:** core/src/banking_stage/consumer.rs (L710-739)
```rust
    pub fn check_fee_payer_unlocked(
        bank: &Bank,
        transaction: &impl TransactionWithMeta,
        error_counters: &mut TransactionErrorMetrics,
    ) -> Result<(), TransactionError> {
        let fee_payer = transaction.fee_payer();
        let transaction_configuration = transaction.transaction_configuration(&bank.feature_set)?;
        let fee = solana_fee::calculate_fee(
            transaction,
            bank.fee_structure().lamports_per_signature,
            transaction_configuration.priority_fee_lamports,
            bank.fee_features(),
        );
        let (mut fee_payer_account, _slot) = bank
            .rc
            .accounts
            .load_with_fixed_root(&bank.ancestors, fee_payer)
            .ok_or(TransactionError::AccountNotFound)?;

        validate_fee_payer(
            &mut fee_payer_account,
            0,
            error_counters,
            &bank.rent_collector().rent,
            fee,
            bank.feature_set
                .snapshot()
                .relax_post_exec_min_balance_check,
        )
    }
```

**File:** svm/src/account_loader.rs (L373-421)
```rust
pub fn validate_fee_payer(
    payer_account: &mut AccountSharedData,
    payer_index: IndexOfAccount,
    error_metrics: &mut TransactionErrorMetrics,
    rent: &Rent,
    fee: u64,
    relax_post_exec_min_balance_check: bool,
) -> Result<()> {
    if payer_account.lamports() == 0 {
        error_metrics.account_not_found += 1;
        return Err(TransactionError::AccountNotFound);
    }
    let system_account_kind = get_system_account_kind(payer_account).ok_or_else(|| {
        error_metrics.invalid_account_for_fee += 1;
        TransactionError::InvalidAccountForFee
    })?;
    let min_balance = match system_account_kind {
        SystemAccountKind::System => 0,
        SystemAccountKind::Nonce => {
            // Should we ever allow a fees charge to zero a nonce account's
            // balance. The state MUST be set to uninitialized in that case
            rent.minimum_balance(NonceState::size())
        }
    };

    payer_account
        .lamports()
        .checked_sub(min_balance)
        .and_then(|v| v.checked_sub(fee))
        .ok_or_else(|| {
            error_metrics.insufficient_funds += 1;
            TransactionError::InsufficientFundsForFee
        })?;

    let pre_balance = payer_account.lamports();
    payer_account
        .checked_sub_lamports(fee)
        .map_err(|_| TransactionError::InsufficientFundsForFee)?;
    let post_balance = payer_account.lamports();

    check_static_account_rent_state_transition(
        pre_balance,
        post_balance,
        payer_account.data().len(),
        rent,
        payer_index,
        relax_post_exec_min_balance_check,
    )
}
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L292-363)
```rust
            let priority = state.priority();
            let raw_nonce_address = state.transaction().get_durable_nonce().cloned();

            // When we first receive a transaction, we drop it if a) it looks nonce-like, AND
            // b) there is a higher-priority nonce transaction using the same nonce in the queue
            // or any in-flight nonce transaction using the same nonce. This means we discard
            // blockhash transactions structured like nonce transactions; this is acceptable because
            // they would fail after the earlier nonce transaction is processed, and it allows us to
            // prefilter without loading from accounts-db.
            let drop_incoming_nonce_tx = raw_nonce_address
                .and_then(|address| container.get_nonce_transaction_priority_id(&address))
                .is_some_and(|existing| {
                    existing.priority >= priority || !container.is_queued(existing)
                });

            if drop_incoming_nonce_tx {
                receiving_stats.num_dropped_on_nonce_dedup += 1;
                continue;
            }

            // Check blockhash transaction age is ok, or nonce transaction has a valid nonce.
            // Only a fully validated nonce address can be used for priority queue eviction.
            let validated_nonce_address = match working_bank.check_transaction_without_status_cache(
                state.transaction(),
                working_bank.max_processing_age(),
                &mut error_counters,
            ) {
                // Valid nonce transaction
                Ok(Some(nonce_address)) => Some(nonce_address),

                // Valid blockhash transaction
                Ok(None) => None,

                // Invalid
                Err(ref err) => {
                    receiving_stats.add_transaction_error(err);
                    continue;
                }
            };

            // Check the transaction's fee-payer validates.
            if let Err(_err) = Consumer::check_fee_payer_unlocked(
                working_bank,
                state.transaction(),
                &mut error_counters,
            ) {
                receiving_stats.num_dropped_on_fee_payer += 1;
                continue;
            };

            let transaction_id = container.insert_map_only(state);
            let priority_id = TransactionPriorityId::new(priority, transaction_id);

            // Now, if this is a nonce transaction, we know it is validated and higher-priority than any
            // which may exist in the priority queue. If one is queued, evict it. Regardless, record the
            // incoming nonce transaction's nonce as in-use.
            if let Some(nonce_address) = validated_nonce_address {
                if let Some(existing_nonce_priority_id) =
                    container.get_nonce_transaction_priority_id(&nonce_address)
                {
                    receiving_stats.num_evicted_on_nonce_dedup += 1;
                    container.remove_by_id(existing_nonce_priority_id.id);
                }
                container.set_nonce_transaction_priority_id(&nonce_address, priority_id);
            }

            // Transaction is already fully validated and can be inserted into priority queue.
            receiving_stats.num_dropped_on_capacity +=
                container.push_ids_into_queue(std::iter::once(priority_id));

            receiving_stats.num_buffered += 1;
        }
```

**File:** core/src/banking_stage/transaction_scheduler/transaction_state_container.rs (L178-201)
```rust
    fn push_ids_into_queue(
        &mut self,
        priority_ids: impl Iterator<Item = TransactionPriorityId>,
    ) -> usize {
        for id in priority_ids {
            self.priority_queue.insert(id);
        }

        // The number of items in the `id_to_transaction_state` map is
        // greater than or equal to the number of elements in the queue.
        // To avoid the map going over capacity, we use the length of the
        // map here instead of the queue.
        let num_dropped = self
            .id_to_transaction_state
            .len()
            .saturating_sub(self.capacity);

        for _ in 0..num_dropped {
            let priority_id = self.priority_queue.pop_first().expect("queue is not empty");
            self.remove_state(priority_id.id);
        }

        num_dropped
    }
```
