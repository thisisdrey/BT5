# Q5938: resharding boundary receipt and account migration — flat_storage_resharder.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, state and in-flight receipts for accounts that straddle the new boundary key, when the same transaction is replayable across a reorg at the window edge, and additionally when execution depends on data the witness does not fully determine, reach `start_resharding_blocking_impl` in `chain/chain/src/resharding/flat_storage_resharder.rs` and strand accounts or receipts on the wrong side of the split so their balance is unreachable, breaking the invariant that every account and receipt maps to exactly one child shard after a split, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `chain/chain/src/resharding/flat_storage_resharder.rs` :: `start_resharding_blocking_impl`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: state and in-flight receipts for accounts that straddle the new boundary key; when the same transaction is replayable across a reorg at the window edge; when execution depends on data the witness does not fully determine
- Exploit idea: strand accounts or receipts on the wrong side of the split so their balance is unreachable
- Invariant to test: every account and receipt maps to exactly one child shard after a split
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: test-loop resharding test asserting account and receipt coverage
