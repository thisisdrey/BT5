# Q5854: outgoing metadata group boundaries — scheduler.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a stream of receipts sized to create a maximal number of metadata groups, when the shard oscillates across the congestion threshold every block, and additionally when the target shard's chunk is missing for several consecutive heights, reach `calculate_is_link_allowed` in `runtime/runtime/src/bandwidth_scheduler/scheduler.rs` and make metadata group bookkeeping lose or double-count queued bytes, breaking the invariant that outgoing metadata always reflects the exact bytes and count in the buffer, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `runtime/runtime/src/bandwidth_scheduler/scheduler.rs` :: `calculate_is_link_allowed`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a stream of receipts sized to create a maximal number of metadata groups; when the shard oscillates across the congestion threshold every block; when the target shard's chunk is missing for several consecutive heights
- Exploit idea: make metadata group bookkeeping lose or double-count queued bytes
- Invariant to test: outgoing metadata always reflects the exact bytes and count in the buffer
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: store test asserting metadata equals a full buffer scan
