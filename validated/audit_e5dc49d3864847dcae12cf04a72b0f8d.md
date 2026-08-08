### Title
Unbounded permanent consumption of per-slot `EntryBytesBudget` on `record_transactions` failure - ([File: runtime/src/bank/entry_bytes_budget.rs])

### Summary
`bank.entry_bytes_budget().reserve(entry_bytes)` is called in `Consumer::execute_and_commit_transactions_locked` (core/src/banking_stage/consumer.rs:371-376) before `transaction_recorder.record_transactions` is invoked. If recording subsequently fails (e.g. `PohRecorderError::ChannelFull`), only `Self::remove_added_transaction_costs(bank, &transaction_costs)` is called to unwind cost-tracker state; the reserved entry bytes are never released back to the budget.

### Finding Description
`EntryBytesBudget` (runtime/src/bank/entry_bytes_budget.rs:8-42) only exposes a `reserve()` method that monotonically increments an `AtomicU64` counter (`consumed`) up to `slot_limit`; there is no `release`/`unreserve`/`rollback` method defined anywhere in the struct. In `consumer.rs`:

```
let reserved_bytes = bank.entry_bytes_budget().reserve(entry_bytes)...;
let (record_transactions_summary, record_us) = measure_us!(reserved_bytes.map(|_| {
    self.transaction_recorder.record_transactions(bank.bank_id(), processed_transactions)
}));
...
if let Err(recorder_err) = recording_result {
    Self::remove_added_transaction_costs(bank, &transaction_costs);
    ...
    return ExecuteAndCommitTransactionsOutput { ... commit_transactions_result: Err(recorder_err), ... };
}
```
On the `Err(recorder_err)` path, only transaction cost-tracker state is unwound via `remove_added_transaction_costs`; the `entry_bytes` amount that was already added to `consumed` in `EntryBytesBudget` remains committed permanently for the lifetime of that `Bank`/slot. Since the budget resets only when a new `Bank` is created for the next slot, once `record_transactions` starts failing (e.g., due to `ChannelFull`, which is a transient condition unrelated to slot end — it indicates the channel to the PoH/broadcast pipeline is backed up, not that the slot has ended), repeated large reservations that are never released can push `consumed` toward `slot_limit`, after which subsequent legitimate reservations get `EntryBytesReserveError::ExceedsSlotLimit` → `PohRecorderError::MaxHeightReached`, blocking all further recording for the remainder of the slot even though no bytes were actually written into any PoH entry.

### Impact Explanation
This causes wasted, unreleased consumption of the leader's per-slot record-byte budget on a legitimate, reachable failure path (`ChannelFull`), which is not equivalent to end-of-slot. Repeated occurrences during a slot can exhaust `slot_limit` prematurely, causing `Consumer` to reject all further transaction recording for the rest of that slot (effectively a denial-of-service on block production capacity for that slot) — matching a "block production stall" class bug.

### Likelihood Explanation
Triggering `ChannelFull` specifically requires the internal channel between banking stage and the PoH/broadcast machinery to be saturated — a condition dependent on internal buffer sizing and current cluster/validator load, not something a remote, unstaked/unprivileged QUIC client can reliably or exclusively induce on demand from outside. The audit prompt frames this as a hypothesis ("can an attacker submit maximally-sized transactions repeatedly...") but no evidence was found in this pass that an external, unprivileged attacker can deterministically and repeatedly force `ChannelFull` (as opposed to `MaxHeightReached`, which occurs naturally at slot end and where this leak has no impact since the slot is over) purely by sending transactions over the public TPU. I was unable to fully verify the channel/queue depth configuration and backpressure behavior of the record-transactions channel (i.e., how large it is, whether it can be filled by transaction volume alone under realistic leader throughput) within the scope of this pass, so exploitability by a single unprivileged remote sender within one slot is not established.

### Recommendation
Add a `release`/`unreserve` method to `EntryBytesBudget` that decrements `consumed` (with a saturating subtraction) and call it from the `Err(recorder_err)` branch in `Consumer::execute_and_commit_transactions_locked` alongside `Self::remove_added_transaction_costs`, so a failed `record_transactions` call fully unwinds all reserved slot-scoped resources (both cost-tracker and entry-bytes budget), not just cost-tracker state.

### Proof of Concept
Unit/integration test plan (runtime/src/bank/entry_bytes_budget.rs + core/src/banking_stage/consumer.rs):
1. Construct a `Bank`/`EntryBytesBudget` with a known `slot_limit`.
2. Call `reserve(entry_bytes)` successfully, then simulate `transaction_recorder.record_transactions` returning `Err(PohRecorderError::ChannelFull)` (e.g., by using a `TransactionRecorder` wired to a full/closed channel in a test harness).
3. Assert that after `execute_and_commit_transactions_locked` returns `Err(PohRecorderError::ChannelFull)`, `bank.entry_bytes_budget()`'s internal `consumed` value has been restored to its pre-reserve value (expected fix behavior) — currently it will remain at the post-reserve value, demonstrating the leak.
4. Repeat step 2 in a loop until `consumed` reaches `slot_limit` purely from failed reservations, then assert that a subsequent legitimate `reserve(1)` call now incorrectly fails with `ExceedsSlotLimit` despite zero bytes ever being successfully recorded into PoH — demonstrating the permanent budget starvation for the remainder of the slot.