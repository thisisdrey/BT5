# Q0332: buffered outgoing receipts never drained — receipts_column_helper.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts targeting a shard the attacker keeps at maximum congestion with cheap self-sustaining traffic, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `trie_key` in `core/store/src/trie/receipts_column_helper.rs` and keep a receiver shard permanently rejecting new receipts so queued value is never delivered, breaking the invariant that every buffered receipt is eventually delivered under bounded congestion, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/store/src/trie/receipts_column_helper.rs` :: `trie_key`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts targeting a shard the attacker keeps at maximum congestion with cheap self-sustaining traffic; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: keep a receiver shard permanently rejecting new receipts so queued value is never delivered
- Invariant to test: every buffered receipt is eventually delivered under bounded congestion
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: test-loop test holding a shard congested and measuring drain progress
