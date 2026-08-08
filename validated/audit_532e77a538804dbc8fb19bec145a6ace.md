### Title
Priority-floor bypass via inconsistent cost calculation between sigverify-stage bytes-path and scheduler typed-path - ([File: core/src/sigverify.rs and core/src/transaction_priority.rs])

### Summary
`core/src/transaction_priority.rs::calculate_priority_and_cost` computes a transaction's priority as `P = R * MULTIPLIER / (C + 1)`, where `R` (reward) and `C` (cost) are computed independently via `solana_fee::calculate_fee_details` and `CostModel::calculate_cost_for_executed_transaction`. This same helper is called from two different code paths: `calculate_priority_from_bytes` (used by the sigverify-stage/`pf-floor` rejection check on raw, unsanitized-fee-context packet bytes per `core/src/sigverify.rs`) and the scheduler's typed transaction path (`core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs`). A code comment and unit test (`floor_priority_from_bytes_matches_typed_path`, `core/src/transaction_priority.rs:167-192`) explicitly assert these two paths "must agree," which signals that developers are aware this consistency is safety-critical for the pf-floor mechanism.

### Finding Description
The pf-floor check exists to reject/drop low-priority transactions early (at sigverify, before they consume banking-stage/QoS resources), based on a priority value computed from unsanitized `data(..)` bytes. The real scheduling decision — which thread a transaction lands on, and whether it beats other queued transactions — is computed later using the fully sanitized `RuntimeTransaction` typed view with `CostModel::calculate_cost_for_executed_transaction`. [1](#0-0) 

Both the floor check and the scheduler-queue priority route through the identical `calculate_priority_and_cost` function [2](#0-1) , and the integer division `reward.saturating_mul(MULTIPLIER).saturating_div(cost.saturating_add(1))` truncates. Because `cost` is derived from `CostModel::calculate_cost_for_executed_transaction`, which reads `transaction_configuration.compute_unit_limit` and `loaded_accounts_data_size_limit` off of parsed/sanitized transaction metadata [3](#0-2) , any divergence between the bytes-derived and typed-derived `TransactionConfiguration` (e.g., due to differing sanitize/clamping rules on the two code paths, or floating truncation boundary effects on the fee/cost ratio) would cause the floor-check priority (bytes path) to differ from the priority used for actual scheduling (typed path) — the same class of bug as the reported SwapNet issue, where two divisions of the same underlying quantity, performed via different intermediate paths, silently diverge due to truncation.

The existing regression test only proves parity for one specific example (a `compute_unit_price = 100` transfer with zero base fee) [4](#0-3) ; it does not constitute a general proof that all sanitize/clamping edge cases (e.g., truncation near integer boundaries of `reward * MULTIPLIER`, or discrepancies between `CostModel::estimate_cost` used in `core/src/forwarding_stage.rs::calculate_priority` and `CostModel::calculate_cost_for_executed_transaction` used in `transaction_priority.rs`) always agree.

### Impact Explanation
If bytes-path and typed-path priority computations diverge for certain crafted transactions (e.g., ones intentionally engineered to sit at a rounding boundary of the `saturating_div` truncation), an attacker could construct a transaction that:
- Computes a priority above the pf-floor threshold on the bytes path (passing sigverify screening), while
- Computing a lower true priority/cost profile in the scheduler, effectively evading intended QoS-based deprioritization, or
- The reverse: a legitimately high-priority, high-paying transaction could be incorrectly dropped at the sigverify pf-floor stage even though it would have scored acceptably at the scheduler.

This is a QoS-evasion-class issue (bounded — it does not by itself cause a panic, deadlock, unbounded memory or an invalid recorded block), but it undermines the fee-market/priority mechanism that is core to banking-stage transaction admission and scheduling fairness.

### Likelihood Explanation
Both computations feed off the *same* underlying `calculate_priority_and_cost` function, so under normal conditions they should agree — this substantially lowers likelihood relative to the original report (which had two genuinely different formulas). However, the fact that a dedicated invariant test exists at all (`floor_priority_from_bytes_matches_typed_path`) indicates the developers considered this a real risk surface, and the test only checks a single non-boundary case rather than exhaustively verifying the division-truncation boundary conditions across the full input space (extreme compute-unit-limits, loaded-accounts-data-size clamping differences, etc.). Without being able to fully trace every sanitize/clamping code path shared between `calculate_priority_from_bytes` (raw bytes → `RuntimeTransaction` via `SanitizedTransactionView`) and the scheduler's ingestion path (`receive_and_buffer.rs`), I cannot conclusively prove a concrete divergent input exists — this is an unverified/possible-but-unconfirmed root cause.

### Recommendation
- Audit all callers of `calculate_priority_and_cost` (sigverify pf-floor and scheduler priority-queue ordering) to guarantee they always operate on identical, fully-sanitized `TransactionConfiguration` values (not partially-derived ones from raw bytes) before the priority division is performed.
- Replace ad-hoc equality unit tests with property-based tests that assert `calculate_priority_from_bytes(bank, bytes) == calculate_priority_and_cost(bank, sanitized_tx, config)` across randomized/boundary compute-unit-limits, fees, and loaded-accounts-data-size values, specifically targeting values near `saturating_div` truncation boundaries.
- Consider deriving the pf-floor priority directly from the already-sanitized typed transaction (reusing the exact same `TransactionConfiguration` instance) rather than recomputing it independently from bytes, eliminating the possibility of divergence entirely.

### Proof of Concept
Not able to produce a concrete divergent input with the tools available (read-only code search); the analysis is based on structural code review showing that:
1. `calculate_priority_from_bytes` (bytes path, used at sigverify per pf-floor logic in `core/src/sigverify.rs`) and the scheduler's typed priority computation both truncate-divide via `calculate_priority_and_cost` [5](#0-4) .
2. A dedicated consistency test exists (`floor_priority_from_bytes_matches_typed_path`) but only checks one input, not the full boundary space [4](#0-3) .

A background Devin session with full repo/build access would be needed to fuzz `calculate_priority_from_bytes` vs. the scheduler's typed-path priority across many `compute_unit_limit`/`compute_unit_price`/`loaded_accounts_data_size_limit` combinations to confirm whether a concrete divergent transaction can be constructed (i.e., to move this from "possible" to "proven").

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
