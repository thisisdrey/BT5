### Title
QoS priority-floor bypass via ALT/V0 cost mismatch between sigverify's bytes-path and scheduler's typed-path priority calculation - ([File: core/src/sigverify.rs])

### Summary
The reported bug class is a decoupling between the data used to *validate/estimate* an operation and the data actually used when the operation is *scheduled/executed*. Agave has a structurally analogous decoupling in the banking-stage admission pipeline: the sigverify-side priority-floor filter (`apply_priority_floor_to_batch` / `calculate_priority_from_bytes`) computes a transaction's priority/cost from an *unresolved* `SanitizedTransactionView` (static account keys only, no address-lookup-table resolution), while the scheduler's actual admission/priority-queue path (`TransactionViewReceiveAndBuffer::try_handle_packet` → `translate_to_runtime_view`) computes priority/cost from a fully *resolved* `ResolvedTransactionView` that includes ALT-loaded write/readonly accounts. Both paths feed the same `calculate_priority_and_cost` formula, but with different account-key/write-lock inputs, so for V0 transactions using address lookup tables the two computed costs (and therefore priorities) can diverge.

### Finding Description
`calculate_priority_from_bytes` (used by sigverify to check the shared `SchedulerPriorityFloor`) builds only a statically-loaded view: [1](#0-0) 
This never resolves address-lookup-table (ALT) references — it stops at `RuntimeTransaction<SanitizedTransactionView<_>>`, which only sees the *static* account keys, not accounts loaded dynamically via `MessageAddressTableLookup`.

`apply_priority_floor_to_batch` calls this bytes-path function directly on raw packet data to decide whether to drop a packet before it ever reaches the scheduler: [2](#0-1) 

By contrast, the scheduler's real admission path resolves ALT addresses before computing priority/cost: [3](#0-2) [4](#0-3) 

`calculate_priority_and_cost` derives cost through `CostModel::calculate_cost_for_executed_transaction`, which counts `num_write_locks()`/account keys from whatever `SVMStaticMessage` is passed in: [5](#0-4) 

For a legacy/static-only transaction, both the bytes-path and the typed/resolved path agree (the repo has an explicit regression test asserting this — `floor_priority_from_bytes_matches_typed_path`): [6](#0-5) 
However, that test only exercises a transaction with a `Message::new` legacy message and no ALT lookups. For a V0 transaction with `MessageAddressTableLookup`s, the bytes-path (`calculate_priority_from_bytes`) has no visibility into the extra writable/readonly accounts that will be loaded from the ALT — it only sees the static keys — whereas the scheduler path (`translate_to_runtime_view`, which produces a `ResolvedTransactionView` with `loaded_addresses`) includes those ALT-resolved accounts in `write_lock_cost`/`num_write_locks()`. This is exactly the pattern reported in the external report: the value used for one validation/estimation step (bytes-path priority-floor check) is derived from an input set that is not the same as — and can be *cheaper* than — the input set the *actual* downstream admission path (`translate_to_runtime_view` + `calculate_priority_and_cost`) uses.

### Impact Explanation
Because `priority = reward * MULTIPLIER / (cost + 1)`, understating `cost` at the sigverify stage (by omitting ALT-loaded write locks) *inflates* the bytes-path priority estimate relative to the true, fully-resolved cost that the scheduler will later compute. An attacker can construct a V0 transaction that references many additional writable accounts through an address lookup table (increasing the real `write_lock_cost`/`programs_execution_cost` inputs) while keeping the static message small. Such a transaction would appear to have a higher priority at the sigverify priority-floor check than it truly has, allowing it to bypass the QoS/backpressure priority floor that is meant to reject low-fee spam once the scheduler buffer is saturated. This is a QoS-evasion class issue: transactions that should be filtered out under load are admitted for full signature verification and further processing, consuming validator resources that the priority-floor mechanism exists specifically to protect.

### Likelihood Explanation
The two priority calculation paths are maintained in parallel by design (documented in code comments as "bytes-path" vs "typed-path" and covered by an equivality test), meaning any future divergence in what each path resolves (ALTs being the clearest current example, since the bytes-path deliberately never loads them) directly produces the exact mismatch class described. The existing test (`floor_priority_from_bytes_matches_typed_path`) only covers the non-ALT case, so there is no automated guard against the ALT-caused divergence, making this readily reachable by any user submitting an unprivileged V0 transaction with lookup tables during periods of scheduler saturation (when the floor is non-zero and active).

### Recommendation
Either make `calculate_priority_from_bytes` conservative when a transaction has address-table lookups it cannot resolve without bank state (e.g., treat unresolved-ALT transactions as automatically above/below the floor consistently, or resolve ALTs using the working bank before computing the floor-check cost), or ensure `apply_priority_floor_to_batch` and the scheduler admission path always use account-key sets computed the same way (both resolved or both static) so the priority ordering used for admission control cannot be gamed by shifting cost into ALT-loaded accounts invisible to the floor check. At minimum, extend `floor_priority_from_bytes_matches_typed_path`-style tests to cover V0 transactions with address-lookup-table writable accounts to detect any priority disagreement between the two paths.

### Proof of Concept
1. Construct a V0 `VersionedTransaction` whose static message contains only the fee payer and a cheap instruction (e.g., a `nop`/tiny system instruction), but which references an address lookup table with many writable indexes (e.g., near `MAX_TX_ACCOUNT_LOCKS`).
2. Send this transaction while the scheduler's buffer is saturated so `SchedulerPriorityFloor::get()` returns a non-zero floor (see `core/src/banking_stage/transaction_scheduler/scheduler_controller.rs`, `SaturationState`).
3. At sigverify, `apply_priority_floor_to_batch` calls `calculate_priority_from_bytes`, which computes cost/priority from only the static account keys (no ALT resolution) — yielding an inflated priority that clears the floor, per `core/src/transaction_priority.rs:73-88` and `core/src/sigverify.rs:413-440`.
4. Once admitted to the scheduler, `translate_to_runtime_view`/`try_handle_packet` resolves the ALT and recomputes the *true* (lower) priority/cost including the extra writable accounts (`core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs:370-455`), showing the transaction's real priority is below the floor that should have rejected it at the sigverify stage.

**Note on confidence**: I was unable to actually run or unit-test this scenario, so this is a structural/code-level analysis rather than a confirmed live exploit; the magnitude of the cost delta introduced purely via ALT writable-account count (relative to `WRITE_LOCK_UNITS` in the cost model) would need to be validated empirically to confirm it is large enough to meaningfully change floor admission decisions in practice.

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

**File:** core/src/transaction_priority.rs (L73-88)
```rust
pub(crate) fn calculate_priority_from_bytes(bank: &Bank, data: &[u8]) -> Option<u64> {
    let view = SanitizedTransactionView::try_new_sanitized(data, &sanitize_config()).ok()?;
    let runtime_tx = RuntimeTransaction::<SanitizedTransactionView<_>>::try_new(
        view,
        MessageHash::Compute,
        None,
    )
    .ok()?;
    let transaction_configuration = runtime_tx
        .transaction_configuration(&bank.feature_set)
        .ok()?;
    let (priority, _cost) =
        calculate_priority_and_cost(bank, &runtime_tx, &transaction_configuration);

    Some(priority)
}
```

**File:** core/src/transaction_priority.rs (L167-192)
```rust
    #[test]
    fn floor_priority_from_bytes_matches_typed_path() {
        // The bytes-path and the typed-path must agree on the same packet,
        // since the scheduler-side queue priority is computed via the typed
        // path and the sigverify-side floor check via the bytes path.
        let (bank, mint) = test_bank();
        let bytes = make_tx_bytes(&mint, bank.last_blockhash(), 100);

        let from_bytes = priority_from(&bank, &bytes);

        let view =
            SanitizedTransactionView::try_new_sanitized(&bytes[..], &sanitize_config()).unwrap();
        let runtime_tx = RuntimeTransaction::<SanitizedTransactionView<_>>::try_new(
            view,
            MessageHash::Compute,
            None,
        )
        .unwrap();
        let transaction_configuration = runtime_tx
            .transaction_configuration(&bank.feature_set)
            .unwrap();
        let (from_typed, _cost) =
            calculate_priority_and_cost(&bank, &runtime_tx, &transaction_configuration);

        assert_eq!(from_bytes, from_typed);
    }
```

**File:** core/src/sigverify.rs (L413-440)
```rust
fn apply_priority_floor_to_batch(
    batch: &mut PacketBatch,
    floor: u64,
    bank: &Bank,
) -> (usize, bool) {
    let mut dropped: usize = 0;
    let mut any_kept = false;
    for mut packet in batch.iter_mut() {
        if packet.meta().discard() {
            continue;
        }
        let Some(data) = packet.data(..) else {
            // Zero-length or otherwise unreadable: leave to downstream
            // stages to reject.
            any_kept = true;
            continue;
        };
        // Unparseable packets are kept and left for downstream rejection.
        match calculate_priority_from_bytes(bank, data) {
            Some(priority) if priority <= floor => {
                packet.meta_mut().set_discard(true);
                dropped = dropped.saturating_add(1);
            }
            _ => any_kept = true,
        }
    }
    (dropped, !any_kept)
}
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L370-405)
```rust
    fn try_handle_packet(
        bytes: Bytes,
        root_bank: &Bank,
        working_bank: &Bank,
        transaction_account_lock_limit: usize,
        sanitize_config: &SanitizeConfig,
        filter_keys: &HashSet<Pubkey>,
    ) -> Result<TransactionViewState, PacketHandlingError> {
        let (view, deactivation_slot) = translate_to_runtime_view(
            bytes,
            root_bank,
            transaction_account_lock_limit,
            sanitize_config,
        )?;

        if !filter_keys.is_empty()
            && view
                .account_keys()
                .iter()
                .any(|key| filter_keys.contains(key))
        {
            return Err(PacketHandlingError::FilterKey);
        }

        let Ok(transaction_configuration) =
            view.transaction_configuration(&working_bank.feature_set)
        else {
            return Err(PacketHandlingError::ComputeBudget);
        };

        let max_age = calculate_max_age(root_bank.epoch(), deactivation_slot, root_bank.slot());
        let (priority, cost) =
            calculate_priority_and_cost(working_bank, &view, &transaction_configuration);

        Ok(TransactionState::new(view, max_age, priority, cost))
    }
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L411-455)
```rust
pub(crate) fn translate_to_runtime_view<D: TransactionData>(
    data: D,
    bank: &Bank,
    transaction_account_lock_limit: usize,
    sanitize_config: &SanitizeConfig,
) -> Result<(RuntimeTransaction<ResolvedTransactionView<D>>, u64), PacketHandlingError> {
    // Parsing and basic sanitization checks
    let Ok(view) = SanitizedTransactionView::try_new_sanitized(data, sanitize_config) else {
        return Err(PacketHandlingError::Sanitization);
    };

    let Ok(view) = RuntimeTransaction::<SanitizedTransactionView<_>>::try_new(
        view,
        MessageHash::Compute,
        None,
    ) else {
        return Err(PacketHandlingError::Sanitization);
    };

    // Discard non-vote packets if in vote-only mode.
    if bank.vote_only_bank() && !view.is_simple_vote_transaction() {
        return Err(PacketHandlingError::Sanitization);
    }

    if usize::from(view.total_num_accounts()) > transaction_account_lock_limit {
        return Err(PacketHandlingError::LockValidation);
    }

    let (loaded_addresses, deactivation_slot) = load_addresses_for_view(&view, bank)?;

    let Ok(view) = RuntimeTransaction::<ResolvedTransactionView<_>>::try_new(
        view,
        loaded_addresses,
        bank.get_reserved_account_keys(),
    ) else {
        return Err(PacketHandlingError::Sanitization);
    };

    // Validate no duplicate accounts (must be after resolution to catch ALT duplicates)
    if validate_account_locks(view.account_keys(), transaction_account_lock_limit).is_err() {
        return Err(PacketHandlingError::LockValidation);
    }

    Ok((view, deactivation_slot))
}
```
