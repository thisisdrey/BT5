# Q1548: resharding boundary receipt and account migration — chunk_validation.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, state and in-flight receipts for accounts that straddle the new boundary key, when transaction conversion cost alone approaches the chunk gas limit, reach `validate_receipt_proof` in `chain/chain/src/stateless_validation/chunk_validation.rs` and strand accounts or receipts on the wrong side of the split so their balance is unreachable, breaking the invariant that every account and receipt maps to exactly one child shard after a split, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `chain/chain/src/stateless_validation/chunk_validation.rs` :: `validate_receipt_proof`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: state and in-flight receipts for accounts that straddle the new boundary key; when transaction conversion cost alone approaches the chunk gas limit
- Exploit idea: strand accounts or receipts on the wrong side of the split so their balance is unreachable
- Invariant to test: every account and receipt maps to exactly one child shard after a split
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: test-loop resharding test asserting account and receipt coverage
