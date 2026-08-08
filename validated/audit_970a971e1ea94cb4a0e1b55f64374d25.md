### Title
Priority-floor drop path and scheduler priority path use different `Bank` snapshots for the same fee/cost computation, causing inconsistent (evadable) load-shedding — ([File: core/src/sigverify.rs])

### Summary
`core/src/transaction_priority.rs` implements two supposedly-equivalent priority computations that are explicitly documented and unit-tested to "agree": a bytes-path (`calculate_priority_from_bytes`) used by the sigverify workers to pre-verify-drop low priority packets, and a typed-path (`calculate_priority_and_cost`) used by the transaction scheduler to compute a transaction's real queue priority/cost. [1](#0-0)  Both paths feed the same formula, but each is invoked with whatever `Bank` its caller happens to hold at that moment: the sigverify path uses `sharable_banks.working()` at the instant a packet batch is processed, [2](#0-1)  while the scheduler path is executed later against the bank state current at scheduling time in the banking stage. This is the same bug class as the RToken report: one code path derives a value from a live/authoritative source (`ILendingPool.getNormalizedIncome()` / here, the live working bank's `fee_structure`, `fee_features`, `feature_set`), while a parallel path (`transferFrom` / here, `calculate_priority_and_cost` invoked from a different pipeline stage) can observe a different, potentially stale, snapshot of the same conceptual quantity.

### Finding Description
`calculate_priority_and_cost` derives priority from `bank.fee_structure().lamports_per_signature`, `bank.fee_features()`, and `bank.feature_set`, all of which change across slot/bank boundaries (fee-rate governor updates, feature activations, etc.). [3](#0-2)  `calculate_priority_from_bytes` calls this same function but is only ever invoked from `apply_priority_floor_to_batch` in the sigverify worker, using the *sigverify-stage* working bank captured at `run_transaction_task` time. [4](#0-3) 

The scheduler, by contrast, re-derives the transaction's real priority/cost using the bank in effect when the container/receive-and-buffer path processes the same transaction — which is a distinct call site, at a distinct (later) point in time, potentially against a different `Bank` object (e.g., after a bank rotation or fee-parameter change). The codebase itself acknowledges this invariant is required, not guaranteed by construction: the only protection is a unit test, `floor_priority_from_bytes_matches_typed_path`, which pins both computations to the *same* bank instance and therefore cannot detect the cross-bank divergence: [1](#0-0) 

```
// The bytes-path and the typed-path must agree on the same packet,
// since the scheduler-side queue priority is computed via the typed
// path and the sigverify-side floor check via the bytes path.
```

Because `sharable_banks.working()` is read independently and asynchronously in a hot worker loop (`SigVerifyWorkerPool::worker_iteration` → `run_transaction_task`) relative to whatever bank the scheduler later scores the transaction against, the two priority numbers used respectively to (a) decide whether to drop a packet before verification and (b) decide the transaction's real position in the fee-market queue can diverge whenever `lamports_per_signature`/fee features/feature_set change between the two evaluations. [5](#0-4) 

### Impact Explanation
The priority floor exists specifically as a load-shedding/QoS mechanism: when the scheduler's queue is saturated, it publishes a floor and sigverify workers drop at-or-below-floor packets before spending CPU on signature verification. [6](#0-5)  If the bank snapshot used to compute the bytes-path priority differs from the bank snapshot the scheduler will use once the transaction reaches the container, an attacker (or ordinary network jitter around a fee-parameter/feature-activation boundary) can craft transactions whose `calculate_priority_from_bytes` value (evaluated against one bank) exceeds the floor and is therefore admitted through sigverify, while the actual value later assigned by `calculate_priority_and_cost` (evaluated against a different bank) is lower than intended — or vice versa, causing legitimate high-fee transactions to be dropped pre-verification because the floor check used a stale/different bank's fee parameters. Either direction defeats the intended purpose of the floor: it either lets low-value spam bypass load-shedding under saturation (QoS evasion) or incorrectly discards paying users' transactions before they ever reach the scheduler (unfair denial of service against a subset of legitimate senders), since floor-dropped packets are marked discarded and never forwarded to banking stage. [7](#0-6) 

### Likelihood Explanation
This does not require any privileged role — it only requires the attacker to submit ordinary QUIC/TPU transactions timed around a slot/bank boundary where `fee_structure`/`fee_features`/`feature_set` values change, a condition that occurs naturally on the live cluster (not a contrived mock), unlike the original RToken PoC which required manually setting an internal variable. The dependency is on cross-bank timing between the sigverify worker pool (`sharable_banks.working()`) and the scheduler's later evaluation, which is a normal, unprivileged, remotely-triggerable race rather than a theoretical construct.

### Recommendation
Ensure both the sigverify-stage floor check and the scheduler's queue-priority computation are evaluated against the identical `Bank`/fee-parameter snapshot for a given transaction — e.g., have the scheduler publish the exact bank (or a versioned snapshot of the fee parameters it used) alongside `SchedulerPriorityFloor`, and have `apply_priority_floor_to_batch` use that same snapshot instead of independently fetching `sharable_banks.working()`. Alternatively, tolerate divergence by using a conservative (e.g., minimum-across-recent-banks) floor rather than a live recomputation, so a bank-parameter change cannot let below-floor traffic through nor incorrectly reject above-floor traffic.

### Proof of Concept
Conceptual reproduction (network-timing based, not requiring internal state mutation):
1. Bank N has `fee_rate_governor`/feature-set F1; bank N+1 (next slot) activates a feature or changes `lamports_per_signature` to F2, which changes the `reward` computed inside `calculate_priority_and_cost`. [3](#0-2) 
2. An attacker sends a batch of transactions with `compute_unit_price` set precisely at the current floor threshold, timed to arrive at the sigverify worker while `sharable_banks.working()` still returns bank N (pre-transition), so `calculate_priority_from_bytes` computes a priority just above the floor and the packet is admitted. [8](#0-7) 
3. By the time the transaction reaches the scheduler/container, the working bank has advanced to N+1 with different fee parameters; `calculate_priority_and_cost` now yields a materially different priority than what was used for the admission decision, demonstrating the two paths produced inconsistent results for the same packet — exactly the scenario the existing test `floor_priority_from_bytes_matches_typed_path` only verifies for the degenerate single-bank case and does not cover. [1](#0-0)

### Citations

**File:** core/src/transaction_priority.rs (L36-53)
```rust
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

**File:** core/src/sigverify.rs (L60-67)
```rust
    deduper: Arc<Deduper<2, [u8]>>,
    stats: SigVerifyWorkerStats,
    /// Scheduler-published priority floor: when saturated, the scheduler publishes
    /// the queue-min transaction's priority and workers drop at-or-below-floor
    /// arrivals here, ahead of signature verification. `None` disables the
    /// check (e.g. for the vote worker, which is governed by a separate
    /// priority policy in banking stage).
    priority_floor: Option<Arc<SchedulerPriorityFloor>>,
```

**File:** core/src/sigverify.rs (L296-324)
```rust
        if discard_or_dedup_fail as usize == batch_len {
            return true;
        }

        let working_bank = sharable_banks.working();

        if let Some(floor) = state.priority_floor.as_ref() {
            let floor = floor.get();
            if floor > 0 {
                let ((dropped, all_below), priority_floor_time_us) = measure_us!(
                    apply_priority_floor_to_batch(&mut batch, floor, &working_bank)
                );
                state
                    .stats
                    .total_priority_floor_time_us
                    .fetch_add(priority_floor_time_us as usize, Ordering::Relaxed);
                if dropped > 0 {
                    state
                        .stats
                        .total_dropped_below_priority_floor
                        .fetch_add(dropped, Ordering::Relaxed);
                }
                if all_below {
                    // Entire batch went below-floor: nothing left to verify or
                    // forward.
                    return true;
                }
            }
        }
```

**File:** core/src/sigverify.rs (L413-438)
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
```
