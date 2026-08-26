# Q5388: congestion info recomputation vs stored value — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a burst of cross-shard receipts sized to move a shard exactly across a congestion threshold, when the shard oscillates across the congestion threshold every block, and additionally when the target shard's chunk is missing for several consecutive heights, reach `get_receipt_group_sizes_for_buffer_to_shard` in `runtime/runtime/src/congestion_control.rs` and make the congestion info a chunk carries disagree with the value recomputed from the trie, breaking the invariant that congestion info in the chunk header is exactly the value derivable from shard state, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `get_receipt_group_sizes_for_buffer_to_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a burst of cross-shard receipts sized to move a shard exactly across a congestion threshold; when the shard oscillates across the congestion threshold every block; when the target shard's chunk is missing for several consecutive heights
- Exploit idea: make the congestion info a chunk carries disagree with the value recomputed from the trie
- Invariant to test: congestion info in the chunk header is exactly the value derivable from shard state
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: runtime test comparing stored congestion info against recomputation
