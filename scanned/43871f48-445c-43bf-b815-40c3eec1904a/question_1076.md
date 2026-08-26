# Q1076: distribute_remaining leftover bandwidth accounting — mod.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a shard set where remaining bandwidth after the main pass is maximal and unevenly divisible, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach the primary handler in this file in `runtime/runtime/src/bandwidth_scheduler/mod.rs` and grant more total bandwidth than the link budget, exceeding per-chunk size parameters, breaking the invariant that the sum of all grants never exceeds the configured link and chunk budgets, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `runtime/runtime/src/bandwidth_scheduler/mod.rs` :: primary handler
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a shard set where remaining bandwidth after the main pass is maximal and unevenly divisible; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: grant more total bandwidth than the link budget, exceeding per-chunk size parameters
- Invariant to test: the sum of all grants never exceeds the configured link and chunk budgets
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: property test asserting the grant-sum bound over random shard states
