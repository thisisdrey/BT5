### Title
Priority-floor bypass for v0/ALT transactions due to unresolved-view cost underestimation - ([File: core/src/transaction_priority.rs])

### Summary
`calculate_priority_from_bytes`, used by the sigverify-stage priority floor to cheaply pre-filter packets before signature verification, computes cost/priority from an **unresolved** `SanitizedTransactionView` that has no knowledge of Address Lookup Table (ALT) contents. For v0 transactions that place accounts in an ALT rather than as static keys, this understates `num_write_locks`/loaded-account cost relative to the **resolved** typed-path computation performed later in `receive_and_buffer.rs`, letting an attacker craft transactions whose apparent (bytes-path) priority is inflated well above the true (typed-path) priority.

### Finding Description
`calculate_priority_from_bytes` builds a `RuntimeTransaction<SanitizedTransactionView<_>>` — the un-resolved view — and feeds it straight into `calculate_priority_and_cost`, without ever loading the ALT addresses: [1](#0-0) 

This is invoked from `apply_priority_floor_to_batch` in the sigverify worker, which drops any packet whose computed priority is at or below the scheduler-published floor, before the expensive `ed25519_verify_serial` step: [2](#0-1) 

By contrast, the real (typed) path used by the scheduler resolves ALTs before computing priority/cost. `translate_to_runtime_view` explicitly performs `load_addresses_for_view` (which calls `bank.load_addresses_from_ref` for v0 transactions) and only then produces a `ResolvedTransactionView`, which is passed to `calculate_priority_and_cost`: [3](#0-2) 

`calculate_priority_and_cost` relies on `CostModel::calculate_cost_for_executed_transaction`, which uses `transaction.num_write_locks()` from the `SVMStaticMessage`/`SVMMessage` trait implementation. On the unresolved view (`SanitizedTransactionView`), this trait can only see `static_account_keys()` — accounts referenced only via an ALT are invisible until resolution — while the resolved view's implementation accounts for all loaded (static + ALT) write locks. An attacker can therefore construct a v0 transaction with very few static accounts and dozens of write-locked accounts hidden inside a (self-created, unprivileged) ALT. The bytes-path computes a low cost / inflated priority and passes the floor; the resolved typed-path later computes the true, much higher cost / lower priority. The floor check — the one gate specifically designed to shed cheap, low-value packets before paying for full signature verification and scheduler admission — is defeated for exactly this class of transaction.

The referenced unit test `floor_priority_from_bytes_matches_typed_path` in the same file only exercises a legacy (non-v0, no-ALT) transaction, so it does not catch this divergence: [4](#0-3) 

### Impact Explanation
This is a QoS-evasion / underpriced-pre-fee-work bug: the priority floor is meant to ensure "work spent per packet before a fee is collected is bounded and proportionate" during congestion. An attacker who creates their own ALT (a normal, unprivileged on-chain action) and references many write-locked accounts through it can make transactions with a true near-floor or sub-floor priority pass the sigverify-stage floor check, forcing the node to spend full ed25519 verification and banking-stage scheduling resources on transactions that should have been cheaply rejected pre-sigverify. This does not corrupt state, panic the node, or break signature verification correctness itself — it only defeats the load-shedding floor for a targeted class of packets.

### Likelihood Explanation
Fully attacker-reachable from an unstaked remote client: creating/extending an ALT and submitting v0 transactions referencing it requires no special privileges, staking, or validator control. The transaction size limit (~1232 bytes) still allows referencing dozens of ALT-loaded write-locked accounts via compact address-table-lookup indexes, giving a wide gap between apparent and true write-lock cost. The bug is deterministic and reproducible for any v0/ALT transaction, not a race condition.

### Recommendation
Make the sigverify-stage floor check resolve ALT addresses before computing cost/priority (mirroring `translate_to_runtime_view`/`load_addresses_for_view`), or conservatively account for potential ALT-referenced write locks in the bytes-path cost estimate (e.g., using `num_lookup_tables()`/`message_address_table_lookups()` metadata already visible on the unresolved view) so the floor-path priority is never higher than the true typed-path priority.

### Proof of Concept
Extend the existing differential test in `core/src/transaction_priority.rs` with a v0/ALT case:
```rust
#[test]
fn floor_priority_diverges_for_alt_transactions() {
    let (bank, mint) = test_bank_with_lamports_per_signature(5_000);
    // Create/extend an ALT with N writable addresses (via bank.store_account
    // or a real CreateLookupTable+ExtendLookupTable flow), then build a v0
    // VersionedTransaction whose only static writable account is the fee
    // payer, with N accounts pulled from the ALT as writable locks.
    let bytes = make_v0_tx_bytes_with_alt(&mint, bank.last_blockhash(), /*cu_price=*/1_000, &alt_key, n_accounts);

    let from_bytes = calculate_priority_from_bytes(&bank, &bytes).unwrap();

    // Resolve via the real path used by receive_and_buffer.rs
    let (resolved_view, _deactivation_slot) =
        translate_to_runtime_view(bytes.into(), &bank, MAX_TX_ACCOUNT_LOCKS, &sanitize_config()).unwrap();
    let config = resolved_view.transaction_configuration(&bank.feature_set).unwrap();
    let (from_typed, _cost) = calculate_priority_and_cost(&bank, &resolved_view, &config);

    // Expect equality per the documented invariant; assertion fails,
    // demonstrating from_bytes >> from_typed for ALT-heavy transactions.
    assert_eq!(from_bytes, from_typed);
}
```
Expected result: the assertion fails, with `from_bytes` significantly greater than `from_typed`, confirming the floor check systematically overestimates priority for ALT-referenced write locks and would let such a transaction pass a floor that the true priority should have failed.

### Citations

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

**File:** core/src/sigverify.rs (L413-439)
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
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L394-454)
```rust
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
}

/// Perform sanitization checks and transition from data to an executable
/// [`RuntimeTransaction`]. This additionally returns the minimum slot for
/// ALT deactivation, if any. If no minimum slot, Slot::MAX is returned.
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
```
