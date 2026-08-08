### Title
Priority-floor bypass via malformed compute-budget instructions forces full ed25519 verification of unpriced packets - ([File: core/src/sigverify.rs])

### Summary
`apply_priority_floor_to_batch` treats any packet for which `calculate_priority_from_bytes` returns `None` as `any_kept = true`, i.e. it is never discarded and always proceeds to full `ed25519_verify_serial`. `calculate_priority_from_bytes` fails (returns `None`) whenever `RuntimeTransaction::try_new` or `transaction_configuration()` fails, which can happen for reasons unrelated to basic message/signature validity (e.g. malformed/duplicate compute-budget instructions or invalid v1 transaction-config fields). Such packets still pass the lighter `SanitizedTransactionView::try_new_sanitized` check used by `verify_packet`/`ed25519_verify_serial`, so they receive full, expensive ed25519 signature verification even though they can never acquire real priority and would normally be shed by the floor.

### Finding Description
The scheduler publishes a `SchedulerPriorityFloor` under saturation so that sigverify workers can cheaply drop low/zero-priority packets before paying for signature verification [1](#0-0) . `run_transaction_task` invokes `apply_priority_floor_to_batch` ahead of `ed25519_verify_serial` specifically to skip crypto work for below-floor packets [2](#0-1) .

`apply_priority_floor_to_batch` calls `calculate_priority_from_bytes`; if it returns `Some(priority) <= floor` the packet is discarded, but for *any* other outcome — including `None` — the packet is kept (`any_kept = true`) and forwarded to verification [3](#0-2) .

`calculate_priority_from_bytes` returns `None` if any of: `SanitizedTransactionView::try_new_sanitized`, `RuntimeTransaction::<SanitizedTransactionView<_>>::try_new`, or `.transaction_configuration(&bank.feature_set)` fail [4](#0-3) .

Critically, `RuntimeTransaction::try_new` performs *additional* checks beyond the basic `SanitizedTransactionView` parse — it builds `InstructionMeta` (which parses precompile signature details) and, for legacy/v0 messages, parses compute-budget instructions via `ComputeBudgetInstructionDetails::try_from`, which can fail independently of message/signature well-formedness (e.g., duplicate compute-budget instructions, or for v1 transactions, out-of-range heap size in `try_into_config`) [5](#0-4) [6](#0-5) .

In contrast, the actual signature-verification path (`verify_packet` used by `ed25519_verify_serial`) only calls `SanitizedTransactionView::try_new_sanitized` — it does not construct a `RuntimeTransaction` and does not evaluate compute-budget instructions at all [7](#0-6) . Therefore a packet can be crafted so that:
- `SanitizedTransactionView::try_new_sanitized` succeeds (valid basic structure, valid signature/pubkey counts), so `verify_packet` proceeds to real ed25519 crypto verification for every included signature, and
- `RuntimeTransaction::try_new`/`transaction_configuration()` fails (e.g., two conflicting `SetComputeUnitLimit` instructions, or malformed v1 `TransactionConfig.heap_size`), so `calculate_priority_from_bytes` returns `None`.

Such a packet is unconditionally kept by `apply_priority_floor_to_batch` regardless of the floor value, forcing full CPU-expensive ed25519 verification in `ed25519_verify_serial` on a packet that has no valid priority and will be rejected downstream anyway (in `receive_and_buffer.rs::translate_to_runtime_view`, which performs the same `transaction_configuration` check and rejects with `PacketHandlingError::ComputeBudget`/`Sanitization`) [8](#0-7) .

No unbounded fee or stake is required to produce such packets, and the attacker fully controls the number of (bogus) signatures included, directly controlling the crypto cost imposed per packet.

### Impact Explanation
This is a CPU-exhaustion / QoS-evasion issue in the sigverify pipeline: it defeats the intended purpose of `scheduler_priority_floor`, which exists precisely to shed cheap/worthless packets before the expensive `ed25519_verify_serial` step under leader saturation. An unstaked attacker flooding the TPU with packets engineered to fail `RuntimeTransaction::try_new`/`transaction_configuration` (while still passing the lighter parse used for signature verification) forces every sigverify worker thread to spend full ed25519 verify time on packets that provide zero fee/priority value and are guaranteed to be dropped downstream, exactly the CPU expenditure the floor is meant to eliminate under load.

### Likelihood Explanation
- Preconditions: `scheduler_priority_floor` must be set (`Some(Arc<SchedulerPriorityFloor>)`, wired for the non-vote worker in `SigVerifyStage::new`/`core/src/tpu.rs`) and floor > 0, i.e., leader under saturation — exactly the "supposed backpressure" scenario described.
- Feasibility: crafting a transaction with duplicate/conflicting compute-budget instructions (legacy/v0) or an out-of-range `heap_size` (v1) that still parses as a structurally valid `SanitizedTransactionView` is straightforward and requires no real fee payment, staking, or key compromise — only correctly-shaped bytes sent to the public TPU.
- Repeatable: the attacker can resend arbitrarily many distinct (to avoid dedup) such packets at will while the leader is under load.

### Recommendation
Change `apply_priority_floor_to_batch` so that packets for which `calculate_priority_from_bytes` returns `None` due to a definitively-fatal error (any error other than "not yet resolvable", i.e., failures in `RuntimeTransaction::try_new`/`transaction_configuration`) are also discarded when the floor is active, rather than always being treated as `any_kept = true`. Concretely, have `calculate_priority_from_bytes` distinguish between "packet doesn't parse at all" (leave for downstream, cheap either way) versus "parses as a transaction view but fails deeper sanitization" (unpriceable and rejectable) and discard the latter under a non-zero floor, since such packets will never receive priority and are guaranteed rejection downstream. At minimum, mirror the exact checks used by `verify_packet` (only `SanitizedTransactionView::try_new_sanitized`) inside the None-priority path so no packet that fails `RuntimeTransaction`/`transaction_configuration` sanitization is allowed to reach `ed25519_verify_serial` when the floor is active.

### Proof of Concept
```rust
// core/src/sigverify.rs (test module) or a new integration test in core/src/transaction_priority.rs

#[test]
fn priority_floor_does_not_shed_unpriceable_but_verify_eligible_packets() {
    use {
        solana_compute_budget_interface::ComputeBudgetInstruction,
        solana_message::Message,
        solana_keypair::Keypair,
        solana_signer::Signer,
        solana_system_interface::instruction as system_instruction,
        solana_transaction::{Transaction, versioned::VersionedTransaction},
    };

    let (bank, mint) = test_bank_with_lamports_per_signature(5_000);

    // Craft a transaction with two conflicting compute-unit-limit instructions.
    // This parses fine as a SanitizedTransactionView (valid signature count,
    // valid instruction layout) but fails RuntimeTransaction::try_new's
    // compute-budget parsing (duplicate instruction), so
    // calculate_priority_from_bytes returns None.
    let to = solana_pubkey::Pubkey::new_unique();
    let transfer = system_instruction::transfer(&mint.pubkey(), &to, 1);
    let dup1 = ComputeBudgetInstruction::set_compute_unit_limit(1_000);
    let dup2 = ComputeBudgetInstruction::set_compute_unit_limit(2_000); // duplicate -> error
    let message = Message::new(&[transfer, dup1, dup2], Some(&mint.pubkey()));
    let tx = Transaction::new(&[&mint], message, bank.last_blockhash());
    let bytes = bincode::serialize(&VersionedTransaction::from(tx)).unwrap();

    // 1. Confirm calculate_priority_from_bytes returns None (unpriceable).
    assert!(calculate_priority_from_bytes(&bank, &bytes).is_none());

    // 2. Confirm the same bytes DO pass verify_packet's lighter check
    //    (SanitizedTransactionView::try_new_sanitized), meaning
    //    ed25519_verify_serial will attempt full crypto verification.
    let view = SanitizedTransactionView::try_new_sanitized(&bytes[..], &sanitize_config());
    assert!(view.is_ok(), "packet must pass the check used by verify_packet");

    // 3. Build a batch with a high floor and assert the packet is kept
    //    (any_kept = true), i.e. NOT dropped by apply_priority_floor_to_batch.
    let mut batch = make_packet_batch(&[bytes]); // helper wrapping bytes into a PacketBatch
    let floor = u64::MAX; // even an unreachably high floor cannot drop it
    let (dropped, all_below) = apply_priority_floor_to_batch(&mut batch, floor, &bank);
    assert_eq!(dropped, 0, "expected the unpriceable packet to bypass the floor");
    assert!(!all_below, "expected any_kept=true, forcing full ed25519_verify_serial");
}
```
Expected assertions confirm: (a) the packet is unpriceable, (b) it still passes the check gating full signature verification, and (c) the priority-floor pre-filter fails to discard it regardless of the floor value — demonstrating that an attacker-controlled, fee-less, unpriceable packet forces full CPU-expensive verification even under maximal backpressure.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L333-352)
```rust
    /// Update the scheduler priority floor.
    ///
    /// Semantics: when the retained scheduler buffer is nearly full, drop
    /// arrivals that are at-or-below the current queue-min priority, i.e. no
    /// better than what the bounded scheduler candidate set would evict.
    fn update_scheduler_priority_floor(&mut self, num_dropped_on_capacity: usize) {
        let buffer_size = self.container.buffer_size();
        let saturated = self
            .saturation_state
            .update(buffer_size, num_dropped_on_capacity);
        let priority_floor = if saturated {
            self.container
                .get_min_max_priority()
                .map_or(0, |(min, _)| min)
        } else {
            0
        };

        self.saturation_state.publish_floor(priority_floor);
    }
```

**File:** core/src/sigverify.rs (L300-331)
```rust
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

        let enable_tx_v1 = working_bank.feature_set.snapshot().enable_tx_v1;
        let (_, verify_time_us) = measure_us!(sigverify::ed25519_verify_serial(
            &mut batch,
            reject_non_vote,
            enable_tx_v1,
        ));
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

**File:** runtime-transaction/src/runtime_transaction/transaction_view.rs (L83-112)
```rust
    let InstructionMeta {
        precompile_signature_details,
        instruction_data_len,
    } = InstructionMeta::try_new(transaction.program_instructions_iter())?;

    let signature_details = TransactionSignatureDetails::new(
        u64::from(transaction.num_required_signatures()),
        precompile_signature_details.num_secp256k1_instruction_signatures,
        precompile_signature_details.num_ed25519_instruction_signatures,
        precompile_signature_details.num_secp256r1_instruction_signatures,
    );
    let versioned_transaction_config =
        if let Some(transaction_config_view) = transaction.transaction_config() {
            // NOTE: only txv1 has `transaction_config_view`, which must have been validated for
            // SanitizedTransactionView.
            VersionedTransactionConfiguration::V1(TransactionConfiguration {
                priority_fee_lamports: transaction_config_view.priority_fee_lamports().unwrap_or(0),
                compute_unit_limit: transaction_config_view.compute_unit_limit().unwrap_or(0),
                loaded_accounts_data_size_limit: transaction_config_view
                    .loaded_accounts_data_size_limit()
                    .unwrap_or(0),
                updated_heap_bytes: transaction_config_view
                    .requested_heap_size()
                    .unwrap_or(HEAP_LENGTH as u32),
            })
        } else {
            VersionedTransactionConfiguration::LegacyAndV0(
                ComputeBudgetInstructionDetails::try_from(transaction.program_instructions_iter())?,
            )
        };
```

**File:** runtime-transaction/src/transaction_meta.rs (L139-178)
```rust
    pub(crate) fn try_into_config(
        &self,
        feature_set: &FeatureSet,
    ) -> Result<TransactionConfiguration, TransactionError> {
        match self {
            Self::LegacyAndV0(compute_budget_instruction_details) => {
                let compute_budget_limits = compute_budget_instruction_details
                    .sanitize_and_convert_to_compute_budget_limits(feature_set)?;
                Ok(TransactionConfiguration {
                    updated_heap_bytes: compute_budget_limits.updated_heap_bytes,
                    compute_unit_limit: compute_budget_limits.compute_unit_limit,
                    priority_fee_lamports: compute_budget_limits.get_prioritization_fee(),
                    loaded_accounts_data_size_limit: compute_budget_limits
                        .loaded_accounts_bytes
                        .get(),
                })
            }
            Self::V1(transaction_configuration) => {
                if !(MIN_HEAP_FRAME_BYTES..=MAX_HEAP_FRAME_BYTES)
                    .contains(&transaction_configuration.updated_heap_bytes)
                    || !transaction_configuration
                        .updated_heap_bytes
                        .is_multiple_of(1024)
                {
                    return Err(TransactionError::SanitizeFailure);
                }

                Ok(TransactionConfiguration {
                    updated_heap_bytes: transaction_configuration.updated_heap_bytes,
                    compute_unit_limit: transaction_configuration
                        .compute_unit_limit
                        .min(MAX_COMPUTE_UNIT_LIMIT),
                    priority_fee_lamports: transaction_configuration.priority_fee_lamports,
                    loaded_accounts_data_size_limit: transaction_configuration
                        .loaded_accounts_data_size_limit
                        .min(MAX_LOADED_ACCOUNTS_DATA_SIZE_BYTES.get()),
                })
            }
        }
    }
```

**File:** perf/src/sigverify.rs (L20-63)
```rust
fn verify_packet(packet: &mut PacketRefMut, reject_non_vote: bool, enable_tx_v1: bool) -> bool {
    // If this packet was already marked as discard, drop it
    if packet.meta().discard() {
        return false;
    }

    let Some(data) = packet.data(..) else {
        return false;
    };

    let (is_simple_vote_tx, verified) = {
        let Ok(view) = SanitizedTransactionView::try_new_sanitized(data, &sanitize_config()) else {
            return false;
        };

        if !enable_tx_v1 && matches!(view.version(), TransactionVersion::V1) {
            return false;
        }

        let is_simple_vote_tx = is_simple_vote_transaction_view(&view);
        if reject_non_vote && !is_simple_vote_tx {
            (is_simple_vote_tx, false)
        } else {
            let signatures = view.signatures();
            if signatures.is_empty() {
                (is_simple_vote_tx, false)
            } else {
                let message = view.message_data();
                let static_account_keys = view.static_account_keys();
                let verified = signatures
                    .iter()
                    .zip(static_account_keys.iter())
                    .all(|(signature, pubkey)| signature.verify(pubkey.as_ref(), message));
                (is_simple_vote_tx, verified)
            }
        }
    };

    if is_simple_vote_tx {
        packet.meta_mut().flags |= PacketFlags::SIMPLE_VOTE_TX;
    }

    verified
}
```

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L393-398)
```rust

        let Ok(transaction_configuration) =
            view.transaction_configuration(&working_bank.feature_set)
        else {
            return Err(PacketHandlingError::ComputeBudget);
        };
```
