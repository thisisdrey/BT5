# Q4840: resharding boundary receipt and account migration — flat_storage_resharder.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, state and in-flight receipts for accounts that straddle the new boundary key, when the pool is filled exactly to its bound by many attacker keys, and additionally when the same transaction is replayable across a reorg at the window edge, reach `is_flat_head_on_par_with_chain` in `chain/chain/src/resharding/flat_storage_resharder.rs` and strand accounts or receipts on the wrong side of the split so their balance is unreachable, breaking the invariant that every account and receipt maps to exactly one child shard after a split, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `chain/chain/src/resharding/flat_storage_resharder.rs` :: `is_flat_head_on_par_with_chain`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: state and in-flight receipts for accounts that straddle the new boundary key; when the pool is filled exactly to its bound by many attacker keys; when the same transaction is replayable across a reorg at the window edge
- Exploit idea: strand accounts or receipts on the wrong side of the split so their balance is unreachable
- Invariant to test: every account and receipt maps to exactly one child shard after a split
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: test-loop resharding test asserting account and receipt coverage
