# Q3648: delayed receipt gas accounting when a receipt is re-queued — distribute_remaining.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts that fail gas checks and are pushed back onto the delayed queue repeatedly, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `distribute_remaining_bandwidth` in `runtime/runtime/src/bandwidth_scheduler/distribute_remaining.rs` and burn or refund gas on each requeue so the account is charged more than once for one receipt, breaking the invariant that gas is burned once per receipt execution attempt that does work, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/bandwidth_scheduler/distribute_remaining.rs` :: `distribute_remaining_bandwidth`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts that fail gas checks and are pushed back onto the delayed queue repeatedly; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: burn or refund gas on each requeue so the account is charged more than once for one receipt
- Invariant to test: gas is burned once per receipt execution attempt that does work
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test counting gas burnt across requeues
