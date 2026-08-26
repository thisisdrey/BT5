# Q1707: delayed receipt gas accounting when a receipt is re-queued — receipts_column_helper.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts that fail gas checks and are pushed back onto the delayed queue repeatedly, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `indices` in `core/store/src/trie/receipts_column_helper.rs` and burn or refund gas on each requeue so the account is charged more than once for one receipt, breaking the invariant that gas is burned once per receipt execution attempt that does work, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/receipts_column_helper.rs` :: `indices`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts that fail gas checks and are pushed back onto the delayed queue repeatedly; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: burn or refund gas on each requeue so the account is charged more than once for one receipt
- Invariant to test: gas is burned once per receipt execution attempt that does work
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test counting gas burnt across requeues
