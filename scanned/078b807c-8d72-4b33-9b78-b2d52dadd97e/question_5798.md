# Q5798: receipt size accounting vs actual serialized size — scheduler.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, receipts whose recorded size understates their borsh-serialized bytes, when the shard oscillates across the congestion threshold every block, and additionally when the target shard's chunk is missing for several consecutive heights, reach `is_link_allowed` in `runtime/runtime/src/bandwidth_scheduler/scheduler.rs` and push a chunk past the outgoing size limit while the scheduler believes it is within budget, breaking the invariant that the size the scheduler accounts for equals the serialized size of the receipt, leading to High - Causing network processing nodes to process transactions from the mempool beyond set parameters?

## Target
- File/function: `runtime/runtime/src/bandwidth_scheduler/scheduler.rs` :: `is_link_allowed`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: receipts whose recorded size understates their borsh-serialized bytes; when the shard oscillates across the congestion threshold every block; when the target shard's chunk is missing for several consecutive heights
- Exploit idea: push a chunk past the outgoing size limit while the scheduler believes it is within budget
- Invariant to test: the size the scheduler accounts for equals the serialized size of the receipt
- Expected Immunefi impact: High - Causing network processing nodes to process transactions from the mempool beyond set parameters
- Fast validation: test asserting recorded size equals borsh length for every receipt shape
