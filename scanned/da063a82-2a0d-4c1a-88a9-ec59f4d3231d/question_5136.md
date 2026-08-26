# Q5136: promise callback with a failed predecessor — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a promise chain where the middle promise fails after attaching a deposit to the callback, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `append_action_create_account` in `runtime/runtime/src/receipt_manager.rs` and have the callback's attached deposit neither delivered nor refunded, breaking the invariant that every attached deposit is either delivered to the receiver or refunded to the predecessor, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_create_account`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a promise chain where the middle promise fails after attaching a deposit to the callback; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: have the callback's attached deposit neither delivered nor refunded
- Invariant to test: every attached deposit is either delivered to the receiver or refunded to the predecessor
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test asserting balance conservation across a failed promise chain
