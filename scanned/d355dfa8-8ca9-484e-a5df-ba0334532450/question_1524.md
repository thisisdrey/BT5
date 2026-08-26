# Q1524: congestion threshold hysteresis exploited for stalling — bandwidth_scheduler.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, traffic that oscillates a shard just across the congestion threshold every block, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `is_all_zeros` in `core/primitives/src/bandwidth_scheduler.rs` and keep the shard flip-flopping so useful throughput collapses and blocks are delayed, breaking the invariant that congestion control degrades gracefully and preserves forward progress, leading to High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)?

## Target
- File/function: `core/primitives/src/bandwidth_scheduler.rs` :: `is_all_zeros`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: traffic that oscillates a shard just across the congestion threshold every block; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: keep the shard flip-flopping so useful throughput collapses and blocks are delayed
- Invariant to test: congestion control degrades gracefully and preserves forward progress
- Expected Immunefi impact: High - Temporary freezing of network transactions (chunk/block production delayed 500%+ over recent average)
- Fast validation: test-loop test measuring chunk time under oscillating congestion
