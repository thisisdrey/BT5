# Q5697: bandwidth scheduler determinism across nodes — congestion_info.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a link-saturating traffic pattern that makes grant allocation depend on iteration order, when the shard oscillates across the congestion threshold every block, and additionally when the target shard's chunk is missing for several consecutive heights, reach `is_fully_congested` in `core/primitives/src/congestion_info.rs` and have two honest nodes compute different grants for the same block and diverge, breaking the invariant that the scheduler is a pure deterministic function of the previous state and the block hash, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/congestion_info.rs` :: `is_fully_congested`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a link-saturating traffic pattern that makes grant allocation depend on iteration order; when the shard oscillates across the congestion threshold every block; when the target shard's chunk is missing for several consecutive heights
- Exploit idea: have two honest nodes compute different grants for the same block and diverge
- Invariant to test: the scheduler is a pure deterministic function of the previous state and the block hash
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test running the scheduler twice with shuffled input ordering
