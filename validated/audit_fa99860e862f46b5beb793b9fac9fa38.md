This is confirmed valid: `calculate_priority_from_bytes` fails (returns `None`) not only on truly-unparseable bytes, but on any transaction whose compute-budget instructions are malformed (e.g. duplicate `SetComputeUnitLimit`/`SetComputeUnitPrice`/`RequestHeapFrame` instructions, which yield `TransactionError::DuplicateInstruction`) via `transaction_configuration()` failing. `verify_packet` in `perf/src/sigverify.rs` does not call `transaction_configuration()` at all — it only requires `SanitizedTransactionView::try_new_sanitized` to succeed and the ed25519 signatures to check out. So an attacker can craft a validly-signed, validly-sanitized transaction that deliberately includes a duplicate compute-budget instruction, causing `calculate_priority_from_bytes` to hit `.ok()?` on `transaction_configuration` and return `None`, forcing `any_kept = true` in `apply_priority_floor_to_batch`, bypassing the floor drop and reaching full `ed25519_verify_serial` crypto work.

### Title
Priority-floor bypass via malformed compute-budget instructions forces full ed25519 verification under saturation - (File: `core/src/sigverify.rs`)

### Summary
`apply_priority_floor_to_batch` in `core/src/sigverify.rs` treats any packet for which `calculate_priority_from_bytes` returns `None` as "kept" regardless of the active priority floor. Because `calculate_priority_from_bytes` (`core/src/transaction_priority.rs`) fails whenever `transaction_configuration()` errors — e.g. on duplicate compute-budget instructions — while `verify_packet` (`perf/src/sigverify.rs`) has no such check and only validates basic sanitization plus the ed25519 signature, an attacker can craft cheaply-produced, validly-signed packets that always evade the floor and force full signature verification even when the scheduler is saturated and publishing a non-zero floor.

### Finding Description
`apply_priority_floor_to_batch` (`core/src/sigverify.rs:413-440`) calls `calculate_priority_from_bytes(bank, data)` for every non-discarded packet and only drops a packet when it returns `Some(priority) if priority <= floor`; any other outcome (including `None`) sets `any_kept = true`, keeping the packet in the batch for downstream `ed25519_verify_serial` (`core/src/sigverify.rs:326-331`).

`calculate_priority_from_bytes` (`core/src/transaction_priority.rs:73-88`) requires `SanitizedTransactionView::try_new_sanitized`, `RuntimeTransaction::try_new`, and — critically — `runtime_tx.transaction_configuration(&bank.feature_set)` to all succeed (each guarded with `.ok()?`). `transaction_configuration()` internally calls `ComputeBudgetInstructionDetails::try_from`, which returns `Err(TransactionError::DuplicateInstruction(..))` if the transaction contains two `SetComputeUnitLimit`, `SetComputeUnitPrice`, `RequestHeapFrame`, or `SetLoadedAccountsDataSizeLimit` instructions (`compute-budget-instruction/src/compute_budget_instruction_details.rs:155-189`). This failure makes `calculate_priority_from_bytes` return `None`, even though the transaction is otherwise a perfectly well-formed, validly signed message.

`verify_packet` (`perf/src/sigverify.rs:20-63`), which does the actual expensive `signature.verify()` calls in `ed25519_verify_serial`, never calls `transaction_configuration()`. It only needs `SanitizedTransactionView::try_new_sanitized` to succeed and the signatures over the message to check out cryptographically. Thus a transaction with a duplicate compute-budget instruction sanitizes fine for `verify_packet` but is treated as "unparseable" by the priority-floor logic.

An unprivileged attacker connecting to the TPU can therefore batch-send many small, cheaply-generated transactions each containing two `SetComputeUnitPrice` instructions (trivial to construct, signed with a disposable throwaway keypair — no stake or fee payer balance is even checked at this stage) and a minimal legitimate instruction. Every such packet is kept (`any_kept = true`) regardless of the published floor, and each one proceeds to full `ed25519_verify_serial`/`signature.verify()` work, which is exactly the CPU-expensive step the priority floor is designed to shield against under saturation (see the floor's doc comment at `banking-stage-ingress-types/src/lib.rs:71-75` and `core/src/banking_stage/transaction_scheduler/scheduler_controller.rs:41-46`).

This violates the intended invariant (stated directly in the code comment: "Sigverify drops at-or-below-floor arrivals") that cheap, low-value packets should never force expensive verification once the scheduler is saturated.

### Impact Explanation
This is a resource-exhaustion / QoS-evasion bug: it undermines the leader's priority-floor DoS mitigation. Under saturation, the priority floor exists specifically to let the sigverify stage cheaply drop low-value transactions before spending CPU on `ed25519_verify_serial`. This bug lets an attacker force full-cost signature verification on an unbounded stream of throwaway transactions by simply adding a duplicate compute-budget instruction, defeating the floor's purpose and consuming sigverify worker CPU that should have been reserved for cheap early rejection — a grossly underpriced pre-fee CPU cost, matching the "verification bypass / QoS evasion" bounty category.

### Likelihood Explanation
Fully feasible for a fully unprivileged, unstaked remote attacker: constructing a transaction with a duplicate `ComputeBudgetInstruction::set_compute_unit_price(..)` and a valid signature requires no stake, no special access, and no more than standard TPU/QUIC packet submission. The attack is trivially repeatable at line rate and requires no leader cooperation, no timing tricks, and no privileged action — only the ability to sign an arbitrary transaction with a self-generated keypair and submit it to the TPU port.

### Recommendation
Make the priority-floor check use the same acceptance criteria as `verify_packet`/`ed25519_verify_serial`: either (a) have `apply_priority_floor_to_batch` treat `transaction_configuration()` failures (and other non-signature-related sanitization failures) as priority `0` (drop under any positive floor) rather than "kept for downstream rejection", since such transactions are guaranteed to fail on-chain execution anyway; or (b) perform the compute-budget/config validation inside `verify_packet` itself before doing the expensive signature check, so malformed-config packets are discarded at the same cheap stage regardless of path.

### Proof of Concept
```rust
// core/src/transaction_priority.rs (add to existing #[cfg(test)] mod tests)
#[test]
fn duplicate_compute_budget_instruction_bypasses_priority_floor() {
    let (bank, mint) = test_bank_with_lamports_per_signature(5_000);
    let to = Pubkey::new_unique();
    let transfer = system_instruction::transfer(&mint.pubkey(), &to, 1);
    // Two SetComputeUnitPrice instructions -> DuplicateInstruction in
    // ComputeBudgetInstructionDetails::try_from, so transaction_configuration()
    // fails and calculate_priority_from_bytes returns None, even though the
    // transaction sanitizes fine and can be signature-verified by verify_packet.
    let dup1 = ComputeBudgetInstruction::set_compute_unit_price(1);
    let dup2 = ComputeBudgetInstruction::set_compute_unit_price(1_000_000);
    let message = Message::new(&[transfer, dup1, dup2], Some(&mint.pubkey()));
    let tx = Transaction::new(&[&mint], message, bank.last_blockhash());
    let bytes = bincode::serialize(&VersionedTransaction::from(tx.clone())).unwrap();

    // Priority-floor logic treats this as unparseable -> None -> "kept".
    assert!(calculate_priority_from_bytes(&bank, &bytes).is_none());

    // But it is a validly signed, sanitizable transaction that
    // perf::sigverify::verify_packet (used by ed25519_verify_serial) will
    // NOT reject on sanitization/signature grounds, so it proceeds to full
    // ed25519 verification instead of being dropped by the floor.
    use solana_perf::packet::BytesPacket;
    let packet = BytesPacket::from_data(&tx).unwrap();
    // (Illustrative: verify_packet is private to perf::sigverify; an
    // integration-level PoC would invoke SigVerifyWorkerPool::run_transaction_task
    // with priority_floor set to a value >= any parseable low-value tx's priority,
    // and assert that this batch of duplicate-cu-price packets is NOT marked
    // discarded by apply_priority_floor_to_batch, while a normal low-priority
    // transaction with the same fee/cost profile IS discarded.)
}
```

Fuzz/invariant plan: generate a stream of N packets, half normal low-priority transactions (single `set_compute_unit_price(0)`), half "duplicate-CU-price" transactions of identical size/complexity, with the scheduler priority floor set above both real priorities. Assert that `SigVerifyWorkerStats::total_dropped_below_priority_floor` only ever counts the first group, and that `total_verify_time_us` scales linearly with the fraction of "duplicate-CU-price" packets even as the floor is raised — demonstrating that CPU time in `ed25519_verify_serial` scales with the fraction of these garbage-but-signed packets, contrary to the intended invariant.