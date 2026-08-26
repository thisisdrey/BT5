# Q1771: delayed receipt gas accounting when a receipt is re-queued — outgoing_metadata.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts that fail gas checks and are pushed back onto the delayed queue repeatedly, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `update_on_receipt_pushed` in `core/store/src/trie/outgoing_metadata.rs` and burn or refund gas on each requeue so the account is charged more than once for one receipt, breaking the invariant that gas is burned once per receipt execution attempt that does work, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/outgoing_metadata.rs` :: `update_on_receipt_pushed`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts that fail gas checks and are pushed back onto the delayed queue repeatedly; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: burn or refund gas on each requeue so the account is charged more than once for one receipt
- Invariant to test: gas is burned once per receipt execution attempt that does work
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test counting gas burnt across requeues
