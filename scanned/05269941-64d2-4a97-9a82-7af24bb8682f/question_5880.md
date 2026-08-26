# Q5880: witness limits interacting with congestion-driven receipt batching — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, traffic that makes a chunk process a maximal delayed-receipt batch, when links are saturated across the exact resharding block, and additionally when the interaction crosses a protocol-version upgrade with receipts in flight, reach `settle_state_init_deposit` in `runtime/runtime/src/deterministic_account_id.rs` and produce a witness above the limit purely from queue drain, with no single expensive receipt, breaking the invariant that witness limits bound the aggregate of queue drain plus new receipts, leading to Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard?

## Target
- File/function: `runtime/runtime/src/deterministic_account_id.rs` :: `settle_state_init_deposit`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: traffic that makes a chunk process a maximal delayed-receipt batch; when links are saturated across the exact resharding block; when the interaction crosses a protocol-version upgrade with receipts in flight
- Exploit idea: produce a witness above the limit purely from queue drain, with no single expensive receipt
- Invariant to test: witness limits bound the aggregate of queue drain plus new receipts
- Expected Immunefi impact: Critical - Chunk cannot be validated statelessly (state-witness divergence), permanently stalling or forking the shard
- Fast validation: test measuring witness size while draining a maximal delayed queue
