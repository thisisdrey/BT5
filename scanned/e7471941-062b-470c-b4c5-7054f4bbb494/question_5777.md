# Q5777: delayed receipts inherited across a shard split — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a maximal delayed-receipt queue at the moment of a split, when links are saturated across the exact resharding block, and additionally when the interaction crosses a protocol-version upgrade with receipts in flight, reach `version` in `core/primitives-core/src/deterministic_account_id.rs` and duplicate the queue into both children or drop it from both, breaking the invariant that delayed receipts are partitioned exactly once across children, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/primitives-core/src/deterministic_account_id.rs` :: `version`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a maximal delayed-receipt queue at the moment of a split; when links are saturated across the exact resharding block; when the interaction crosses a protocol-version upgrade with receipts in flight
- Exploit idea: duplicate the queue into both children or drop it from both
- Invariant to test: delayed receipts are partitioned exactly once across children
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: resharding test asserting queue conservation across the split
