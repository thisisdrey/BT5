# Q0129: congestion info recomputation vs stored value — receipt.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a burst of cross-shard receipts sized to move a shard exactly across a congestion threshold, when receipt sizes sit exactly on the bandwidth-request granularity boundary, reach `set_receipt_id` in `core/primitives/src/receipt.rs` and make the congestion info a chunk carries disagree with the value recomputed from the trie, breaking the invariant that congestion info in the chunk header is exactly the value derivable from shard state, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `set_receipt_id`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a burst of cross-shard receipts sized to move a shard exactly across a congestion threshold; when receipt sizes sit exactly on the bandwidth-request granularity boundary
- Exploit idea: make the congestion info a chunk carries disagree with the value recomputed from the trie
- Invariant to test: congestion info in the chunk header is exactly the value derivable from shard state
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: runtime test comparing stored congestion info against recomputation
