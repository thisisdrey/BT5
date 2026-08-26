# Q5041: congestion info for a missing chunk — scheduler.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, traffic timed so the target shard's chunk is missing for several heights, when the shard is driven exactly onto a congestion threshold, and additionally when the shard oscillates across the congestion threshold every block, reach `get_granted_bandwidth` in `runtime/runtime/src/bandwidth_scheduler/scheduler.rs` and have congestion info extrapolated for missing chunks in a way that differs between nodes, breaking the invariant that congestion info for missing chunks is derived deterministically from the last present chunk, leading to High - Unintended chain split (network partition)?

## Target
- File/function: `runtime/runtime/src/bandwidth_scheduler/scheduler.rs` :: `get_granted_bandwidth`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: traffic timed so the target shard's chunk is missing for several heights; when the shard is driven exactly onto a congestion threshold; when the shard oscillates across the congestion threshold every block
- Exploit idea: have congestion info extrapolated for missing chunks in a way that differs between nodes
- Invariant to test: congestion info for missing chunks is derived deterministically from the last present chunk
- Expected Immunefi impact: High - Unintended chain split (network partition)
- Fast validation: test-loop test with forced missing chunks comparing derived congestion info
