### Title
Unbounded, stateless `check_fee_payer_unlocked` re-use allows a single funded account to flood the scheduler buffer with self-priced high-priority junk, forcing `SchedulerPriorityFloor` to pre-sigverify-drop legitimate low-fee transactions - ([File: core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs])

### Summary
`TransactionViewReceiveAndBuffer::handle_packet_batch_message` admits a transaction into the scheduler buffer once `Consumer::check_fee_payer_unlocked` passes, but that check is a stateless, point-in-time balance check against the current bank and does not reserve or decrement the fee payer's balance for the transaction's dwell time in the buffer. A single funded, unstaked account can therefore pass this check for an unbounded number of distinct, self-priced high-priority transactions simultaneously, filling the buffer to `SATURATION_BUFFER_PCT` and forcing `SchedulerController::update_scheduler_priority_floor`/`SaturationState::update` to publish an attacker-chosen high `priority_floor`. This floor is consumed by `SigVerifyWorkerState`/`apply_priority_floor_to_batch` to drop arriving packets *before signature verification*, silently discarding legitimate, real fee-paying, lower-priority user transactions.

### Finding Description
In `core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs`, `handle_packet_batch_message` performs, per incoming packet: nonce dedup (lines 301-310), age/status check (314-330), and then `Consumer::check_fee_payer_unlocked` (333-340) before calling `container.insert_map_only(state)` and `container.push_ids_into_queue` (342-360). [1](#0-0) 

`check_fee_payer_unlocked` in `core/src/banking_stage/consumer.rs` loads the fee payer account from the bank and validates its *current* balance against the *reported* fee for this single transaction — it never marks or reserves those funds against other buffered-but-unexecuted transactions from the same payer: [2](#0-1) 

Unlike the durable-nonce path, which explicitly dedups/evicts competing transactions sharing the same nonce address (lines 295-310 of the same file), there is no analogous limit on the number of concurrently-buffered transactions sharing the same fee-payer key. Priority itself is attacker-controlled via `ComputeBudgetInstruction::set_compute_unit_price`, feeding into `calculate_priority_and_cost` (`core/src/transaction_priority.rs:32-66`), and the fee-payer check only validates that the reported fee could theoretically be paid from the account's balance snapshot, not that it has been debited. Since account locks are only acquired during actual scheduling (`Scheduler::schedule`, via `ThreadAwareAccountLocks`), not at buffer-insertion time, an attacker can submit many uniquely-crafted (to survive the sigverify deduper, which dedups exact packet bytes only) transactions from one funded account, each independently passing `check_fee_payer_unlocked` against the same unchanged balance.

Once buffered near `TOTAL_BUFFERED_PACKETS`, `SchedulerController::run` calls `update_scheduler_priority_floor`, which uses `SaturationState::update(buffer_size, num_dropped_on_capacity)` to flip `saturated = true` once `buffer_size >= SATURATION_BUFFER_PCT` (99%) of capacity, and publishes `priority_floor = container.get_min_max_priority().min` — i.e., the attacker's chosen minimum priority among their flood: [3](#0-2) [4](#0-3) 

That floor is shared with sigverify via `SchedulerPriorityFloor` and applied in `apply_priority_floor_to_batch`, discarding packets computed (from raw bytes, before any signature check) to have priority ≤ floor: [5](#0-4) [6](#0-5) 

Because the buffer-fill cost to the attacker is bounded by a single funded account (reused across an unbounded number of distinct high-priority packets that will mostly fail at actual execution once the real balance is finally consumed by whichever transaction is scheduled/executed first), the attacker sustains saturation and an artificially high floor at negligible, non-scaling economic cost, causing legitimate lower-(but real)-priority transactions to be dropped ahead of signature verification — exactly the invariant the docstring on `SchedulerPriorityFloor` and `update_scheduler_priority_floor` assumes will not happen ("in practice, transactions always have non-zero priorities" backed by real fee-paying capacity).

### Impact Explanation
This is a QoS/anti-spam evasion and pre-fee-collection resource-imbalance bug: work performed against honest users (silent, pre-sigverify rejection) is disproportionate to the bounded, non-scaling cost paid by the attacker (a single funded account, reused indefinitely). It matches the "grossly underpriced pre-fee work" / QoS evasion bounty category — a remote unstaked attacker can deny inclusion consideration to legitimate low-fee transactions on a target leader's TPU without ever having those transactions reach signature verification, let alone execution.

### Likelihood Explanation
Fully reachable by an unstaked remote client: only requires opening a QUIC/UDP connection to a leader's public TPU and sending a stream of distinct transactions (differing blockhash/instruction bytes to bypass the sigverify deduper) all signed by the same funded fee payer, with a high self-declared `compute_unit_price`. No validator, staked-node, or gossip control is needed; the account only needs enough balance to satisfy one transaction's fee snapshot. The behavior is deterministic and reproducible against the existing `SaturationState`/`update_scheduler_priority_floor` logic and is exercisable directly via the existing unit-test harness (`saturation_state_tests`, `receive_and_buffer.rs` tests).

### Recommendation
Track outstanding (buffered-but-unexecuted) fee-payer commitments so that `check_fee_payer_unlocked` accounts for the cumulative fees of all currently-queued transactions from the same fee payer (e.g., a per-fee-payer "reserved balance" ledger maintained by the scheduler/receive-and-buffer path, decremented on eviction/removal), or cap the number of concurrently-buffered transactions per unique fee payer (similar to the existing durable-nonce dedup/eviction mechanism), so that the priority floor derived from buffer saturation reflects genuinely distinct, economically-backed demand rather than reusable-balance flooding.

### Proof of Concept
Rust integration test plan (extends existing `receive_and_buffer.rs` / `scheduler_controller.rs` test infrastructure):
```rust
#[test]
fn test_single_fee_payer_can_saturate_buffer_and_raise_floor() {
    // 1. Set up TransactionViewReceiveAndBuffer + TransactionViewStateContainer with small
    //    capacity (e.g. TEST_CONTAINER_CAPACITY) and a funded mint_keypair with balance just
    //    sufficient to cover ONE high-fee transaction (compute_unit_price = HIGH_FEE).
    // 2. Generate N = TEST_CONTAINER_CAPACITY distinct transactions, all signed by the SAME
    //    mint_keypair fee payer, each with a unique memo/instruction (different bytes to avoid
    //    the sigverify deduper) and identical high compute_unit_price = HIGH_FEE.
    // 3. Feed them through receive_and_buffer_packets. Assert:
    //    - num_dropped_on_fee_payer == 0 for all N (each check is stateless and independently
    //      passes against the same un-debited balance).
    //    - num_buffered ~= N (bounded only by container capacity, not by fee payer's real ability
    //      to pay for N transactions).
    // 4. Drive SchedulerController::update_scheduler_priority_floor / SaturationState::update
    //    with buffer_size >= saturation_watermark(): assert saturated == true and
    //    priority_floor.get() == HIGH_FEE-derived priority.
    // 5. Craft a legitimate, distinct fee payer's transaction with a real, modest priority
    //    (LOW_FEE < HIGH_FEE). Feed it through sigverify's apply_priority_floor_to_batch with
    //    the published floor: assert the packet is marked discard == true, i.e. dropped BEFORE
    //    ed25519 signature verification, despite being a well-formed, fully fundable transaction.
}
```
Expected assertions demonstrate: (a) `check_fee_payer_unlocked` cost does not scale with the number of buffered transactions from one payer, (b) `SaturationState`/`priority_floor` can be driven arbitrarily high using this reusable-balance trick, and (c) a legitimate lower-priority transaction is discarded pre-sigverify as a direct consequence.

### Citations

**File:** core/src/banking_stage/transaction_scheduler/receive_and_buffer.rs (L332-360)
```rust
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

**File:** core/src/banking_stage/transaction_scheduler/scheduler_controller.rs (L90-100)
```rust
    fn update(&mut self, buffer_size: usize, num_dropped_on_capacity: usize) -> bool {
        if self.saturated {
            if buffer_size < self.desaturation_watermark && num_dropped_on_capacity == 0 {
                self.saturated = false;
            }
        } else if buffer_size >= self.saturation_watermark || num_dropped_on_capacity > 0 {
            self.saturated = true;
        }

        self.saturated
    }
```

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

**File:** core/src/sigverify.rs (L300-323)
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
