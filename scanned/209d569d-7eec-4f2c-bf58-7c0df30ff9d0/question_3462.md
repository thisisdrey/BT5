# Q3462: gas refund receipts interacting with congestion rejection — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, refunds generated toward a shard that is rejecting new receipts, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `apply_global_contract_distribution_receipt` in `runtime/runtime/src/global_contracts.rs` and have refunds dropped as if they were ordinary receipts, destroying user balance, breaking the invariant that refunds are never dropped by congestion control, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `apply_global_contract_distribution_receipt`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: refunds generated toward a shard that is rejecting new receipts; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: have refunds dropped as if they were ordinary receipts, destroying user balance
- Invariant to test: refunds are never dropped by congestion control
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test generating refunds toward a fully congested shard
