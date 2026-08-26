# Q2141: promise callback with a failed predecessor — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a promise chain where the middle promise fails after attaching a deposit to the callback, when combined with a DeployContract earlier in the same action list, reach `append_action_add_gas_key_with_full_access` in `runtime/runtime/src/receipt_manager.rs` and have the callback's attached deposit neither delivered nor refunded, breaking the invariant that every attached deposit is either delivered to the receiver or refunded to the predecessor, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_add_gas_key_with_full_access`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a promise chain where the middle promise fails after attaching a deposit to the callback; when combined with a DeployContract earlier in the same action list
- Exploit idea: have the callback's attached deposit neither delivered nor refunded
- Invariant to test: every attached deposit is either delivered to the receiver or refunded to the predecessor
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting balance conservation across a failed promise chain
