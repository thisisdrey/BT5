### Title
Unhandled `panic!` on `PohRecorderError` in `PohService` record path can crash the leader validator - (File: `poh/src/poh_service.rs`)

### Summary
The external report describes a contract that `throw`s when a withdrawal amount exceeds the actually-available balance, instead of clamping the amount to what is available. The closest reachable analog in this codebase is in the PoH recording path: when the amount of work "withdrawn" from the remaining per-slot hash/tick budget exceeds what is actually left (i.e. the working bank has already advanced past the requested `bank_id`/height), `PohRecorder::record()` returns an `Err`, and the calling code in `PohService` responds by `panic!`-ing the whole thread instead of gracefully handling the shortfall.

### Finding Description
`PohRecorder::record()` checks whether there is still an active working bank matching the requested `bank_id` before it can record a batch of transactions. If the working bank has already been cleared/advanced (there is no more "budget" left in the slot for this record), it returns `Err(PohRecorderError::MaxHeightReached)` (or other `PohRecorderError` variants) rather than performing the operation: [1](#0-0) 

Records are only queued into the bounded `RecordSender`/`RecordReceiver` channel under the assumption that the sender-observed `bank_id` matches an "active" bank id tracked independently in `BankIdAllowedInsertions`: [2](#0-1) 

However, this admission check is based on a separate atomic value from the `PohRecorder`'s own `working_bank` state, so there is a window where a record is admitted into the channel as belonging to the "active" bank, but by the time `PohService` actually calls `PohRecorder::record()` on it, the underlying `working_bank` has already changed or been cleared (e.g. it reached `max_tick_height`, or a different bank id took over). This exact race is acknowledged by the codebase's own test comment: *"If receiver/sender interaction is buggy there is a race where the receiver can receive a record after shutdown is called. This can cause PoH to panic because it may receive a record for a bank_id that has already been completed."* [3](#0-2) 

When `PohService` (the thread actually driving PoH, distinct from the higher-level `banking_stage`/`consumer.rs` path that gracefully turns this into `PohRecorderError::MaxHeightReached` and retries the transaction) receives such a record and calls `PohRecorder::record()`, **any** `Err` result — not just the expected `MaxHeightReached` — causes an unconditional `panic!`, both in the blocking `read_record_receiver_and_process` helper: [4](#0-3) 
and in the main hot-path `record_or_hash` function used by the production PoH service loop: [5](#0-4) 

This mirrors the reported bug class precisely: the "withdrawal" (recording of a batch into the remaining PoH slot budget) is attempted after the available budget/state has already been exhausted/changed elsewhere, and instead of treating this as an expected, recoverable condition (as `core/src/banking_stage/consumer.rs` does by converting it into a retry), the `PohService` layer treats *any* error as fatal and panics the process.

### Impact Explanation
A `panic!` inside `PohService`'s core hashing/recording loop crashes the thread; because PoH is a critical, always-required subsystem for a leader/validator to make progress, this effectively halts or crashes the validator process, denying it the ability to produce or validate blocks. This is a concrete node panic triggerable in the normal, unprivileged transaction-recording path (not a validator/operator RPC), which fits the "concrete node panic" acceptance criterion for this scan.

### Likelihood Explanation
Triggering the exact race requires precise timing between record admission (via `RecordSender`/`BankIdAllowedInsertions`) and the actual bank/tick-height transition tracked by `PohRecorder.working_bank`, which the maintainers themselves flagged as a hazard in the shuttle test. It is not directly attacker-controlled with a single crafted packet, but heavy/adversarial transaction load near slot boundaries (an unprivileged user can freely submit high volumes of transactions at the end of a slot) increases the probability of hitting this race window, and the response to any hit is an unconditional crash rather than a bounded/retryable error, unlike the equivalent code path in `consumer.rs`.

### Recommendation
Mirror the handling used in `core/src/banking_stage/consumer.rs`: treat `PohRecorderError` (especially `MaxHeightReached`) returned from `PohRecorder::record()` inside `PohService` as an expected, recoverable condition (e.g., drop/requeue the record and continue) instead of `panic!`-ing. This is the direct analog of the recommended fix in the report — clamp/handle the "withdrawal" to what is actually available rather than unconditionally failing hard when the requested action exceeds the currently available capacity.

### Proof of Concept
Not independently reproduced in this ask-only investigation; the maintainers' own `record_channels.rs` shuttle test `test_sender_shutdown_safety_race` documents that "receiver can receive a record after shutdown is called... can cause PoH to panic because it may receive a record for a bank_id that has already been completed", corroborating the race described above. [6](#0-5)

### Citations

**File:** poh/src/poh_recorder.rs (L362-369)
```rust
            let tick_height = self.tick_height(); // cannot change until next loop iteration.
            let working_bank = self
                .working_bank
                .as_mut()
                .ok_or(PohRecorderError::MaxHeightReached)?;
            if bank_id != working_bank.bank.bank_id() {
                return Err(PohRecorderError::MaxHeightReached);
            }
```

**File:** poh/src/record_channels.rs (L104-123)
```rust
            // Get the current bank_id and allowed insertions.
            // If there are no allowed insertions, the channel is full - just return immediately.
            // If the `record`'s bank_id is different from the current bank_id,
            // return immediately.
            let current_bank_id_allowed_insertions =
                self.bank_id_allowed_insertions.0.load(Ordering::Acquire);
            let (bank_id, allowed_insertions) = (
                BankIdAllowedInsertions::bank_id(current_bank_id_allowed_insertions),
                BankIdAllowedInsertions::allowed_insertions(current_bank_id_allowed_insertions),
            );

            if bank_id == BankIdAllowedInsertions::DISABLED_BANK_ID {
                return Err(RecordSenderError::Shutdown);
            }
            if bank_id != record.bank_id {
                return Err(RecordSenderError::InactiveBankId);
            }
            if allowed_insertions == 0 {
                return Err(RecordSenderError::Full);
            }
```

**File:** poh/src/record_channels.rs (L456-502)
```rust
    #[test]
    fn test_sender_shutdown_safety_race() {
        const NUM_TEST_RUNS: usize = 100;
        shuttle::check_random(
            || {
                let (sender, mut receiver) = record_channels(false);

                const ITERATIONS_PER_RUN: usize = 1024;

                shuttle::thread::spawn(move || {
                    let mut successful_sends = 0;
                    let mut bank_id = 0;
                    let mut had_successful_send = false;
                    while successful_sends < ITERATIONS_PER_RUN {
                        if sender.try_send(test_record(bank_id, 1)).is_ok() {
                            had_successful_send = true;
                            successful_sends += 1;
                        } else if had_successful_send {
                            bank_id += 1;
                            had_successful_send = false;
                        }
                    }
                });

                // If receiver/sender interaction is buggy there is a race where
                // the receiver can receive a record after shutdown is called.
                // This can cause PoH to panic because it may receive a record
                // for a bank_id that has already been completed.
                let mut current_bank_id = 0;
                receiver.restart(current_bank_id);
                let mut receives = 0;
                while receives < ITERATIONS_PER_RUN {
                    if receiver.is_shutdown() && receiver.is_safe_to_restart() {
                        current_bank_id += 1;
                        receiver.restart(current_bank_id);
                    }

                    if let Ok(record) = receiver.try_recv() {
                        assert!(record.bank_id == current_bank_id, "bank_id mismatch!");
                        receives += 1;
                        receiver.shutdown();
                    }
                }
            },
            NUM_TEST_RUNS,
        )
    }
```

**File:** poh/src/poh_service.rs (L314-331)
```rust
        let record = record_receiver.recv_timeout(timeout);
        if let Ok(record) = record {
            match poh_recorder.write().unwrap().record(
                record.bank_id,
                record.mixin,
                record.transactions,
            ) {
                Ok(record_summary) => {
                    if record_receiver
                        .should_shutdown(record_summary.remaining_hashes_in_slot, ticks_per_slot)
                    {
                        record_receiver.shutdown();
                    }
                }
                Err(err) => {
                    panic!("PohRecorder::record failed: {err:?}");
                }
            }
```

**File:** poh/src/poh_service.rs (L469-486)
```rust
                loop {
                    match poh_recorder_l.record(
                        record.bank_id,
                        record.mixin,
                        std::mem::take(&mut record.transactions),
                    ) {
                        Ok(record_summary) => {
                            if record_receiver.should_shutdown(
                                record_summary.remaining_hashes_in_slot,
                                ticks_per_slot,
                            ) {
                                record_receiver.shutdown();
                            }
                        }
                        Err(err) => {
                            panic!("PohRecorder::record failed: {err:?}");
                        }
                    }
```
