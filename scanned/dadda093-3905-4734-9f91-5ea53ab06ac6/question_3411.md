# Q3411: witness limits interacting with congestion-driven receipt batching — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, traffic that makes a chunk process a maximal delayed-receipt batch, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `install_universal_account` in `runtime/runtime/src/universal_account_id.rs` and produce a witness above the limit purely from queue drain, with no single expensive receipt, breaking the invariant that witness limits bound the aggregate of queue drain plus new receipts, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `runtime/runtime/src/universal_account_id.rs` :: `install_universal_account`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: traffic that makes a chunk process a maximal delayed-receipt batch; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: produce a witness above the limit purely from queue drain, with no single expensive receipt
- Invariant to test: witness limits bound the aggregate of queue drain plus new receipts
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: test measuring witness size while draining a maximal delayed queue
