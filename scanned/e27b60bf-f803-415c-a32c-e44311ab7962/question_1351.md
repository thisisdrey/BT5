# Q1351: witness limits interacting with congestion-driven receipt batching — universal_state_init.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, traffic that makes a chunk process a maximal delayed-receipt batch, when a referencing account is deleted while others still reference the code, reach `from_raw` in `core/primitives/src/universal_state_init.rs` and produce a witness above the limit purely from queue drain, with no single expensive receipt, breaking the invariant that witness limits bound the aggregate of queue drain plus new receipts, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `core/primitives/src/universal_state_init.rs` :: `from_raw`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: traffic that makes a chunk process a maximal delayed-receipt batch; when a referencing account is deleted while others still reference the code
- Exploit idea: produce a witness above the limit purely from queue drain, with no single expensive receipt
- Invariant to test: witness limits bound the aggregate of queue drain plus new receipts
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: test measuring witness size while draining a maximal delayed queue
