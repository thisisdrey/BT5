# Q1437: gas refund receipts interacting with congestion rejection — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, refunds generated toward a shard that is rejecting new receipts, when a referencing account is deleted while others still reference the code, reach `receipt_congestion_gas` in `runtime/runtime/src/congestion_control.rs` and have refunds dropped as if they were ordinary receipts, destroying user balance, breaking the invariant that refunds are never dropped by congestion control, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `receipt_congestion_gas`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: refunds generated toward a shard that is rejecting new receipts; when a referencing account is deleted while others still reference the code
- Exploit idea: have refunds dropped as if they were ordinary receipts, destroying user balance
- Invariant to test: refunds are never dropped by congestion control
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test generating refunds toward a fully congested shard
