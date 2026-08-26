# Q5741: distribute_remaining leftover bandwidth accounting — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a shard set where remaining bandwidth after the main pass is maximal and unevenly divisible, when the shard oscillates across the congestion threshold every block, and additionally when the target shard's chunk is missing for several consecutive heights, reach `get_receipt_group_sizes_for_buffer_to_shard` in `runtime/runtime/src/congestion_control.rs` and grant more total bandwidth than the link budget, exceeding per-chunk size parameters, breaking the invariant that the sum of all grants never exceeds the configured link and chunk budgets, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `get_receipt_group_sizes_for_buffer_to_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a shard set where remaining bandwidth after the main pass is maximal and unevenly divisible; when the shard oscillates across the congestion threshold every block; when the target shard's chunk is missing for several consecutive heights
- Exploit idea: grant more total bandwidth than the link budget, exceeding per-chunk size parameters
- Invariant to test: the sum of all grants never exceeds the configured link and chunk budgets
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: property test asserting the grant-sum bound over random shard states
