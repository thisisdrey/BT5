# Q3098: bandwidth scheduler determinism across nodes — receipts_column_helper.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a link-saturating traffic pattern that makes grant allocation depend on iteration order, when receipt sizes sit exactly on the bandwidth-request granularity boundary, and additionally when the shard is driven exactly onto a congestion threshold, reach `to_shard` in `core/store/src/trie/receipts_column_helper.rs` and have two honest nodes compute different grants for the same block and diverge, breaking the invariant that the scheduler is a pure deterministic function of the previous state and the block hash, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/store/src/trie/receipts_column_helper.rs` :: `to_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a link-saturating traffic pattern that makes grant allocation depend on iteration order; when receipt sizes sit exactly on the bandwidth-request granularity boundary; when the shard is driven exactly onto a congestion threshold
- Exploit idea: have two honest nodes compute different grants for the same block and diverge
- Invariant to test: the scheduler is a pure deterministic function of the previous state and the block hash
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test running the scheduler twice with shuffled input ordering
