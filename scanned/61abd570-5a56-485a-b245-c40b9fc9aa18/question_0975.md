# Q0975: delayed receipts inherited across a shard split — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a maximal delayed-receipt queue at the moment of a split, when a referencing account is deleted while others still reference the code, reach `forward_from_buffer_to_shard` in `runtime/runtime/src/congestion_control.rs` and duplicate the queue into both children or drop it from both, breaking the invariant that delayed receipts are partitioned exactly once across children, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `forward_from_buffer_to_shard`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a maximal delayed-receipt queue at the moment of a split; when a referencing account is deleted while others still reference the code
- Exploit idea: duplicate the queue into both children or drop it from both
- Invariant to test: delayed receipts are partitioned exactly once across children
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: resharding test asserting queue conservation across the split
