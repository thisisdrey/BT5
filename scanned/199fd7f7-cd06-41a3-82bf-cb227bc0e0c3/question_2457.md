# Q2457: refund receipt to a deleted or nonexistent predecessor — adapter.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a receipt chain whose predecessor account is deleted before the refund is generated, when combined with a DeployContract earlier in the same action list, reach `view_global_contract_code` in `runtime/runtime/src/adapter.rs` and have the refund minted to nobody, burned twice, or credited to a recreated account, breaking the invariant that refunds are conserved: burned or credited exactly once, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/adapter.rs` :: `view_global_contract_code`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a receipt chain whose predecessor account is deleted before the refund is generated; when combined with a DeployContract earlier in the same action list
- Exploit idea: have the refund minted to nobody, burned twice, or credited to a recreated account
- Invariant to test: refunds are conserved: burned or credited exactly once
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test deleting the predecessor before the refund lands
