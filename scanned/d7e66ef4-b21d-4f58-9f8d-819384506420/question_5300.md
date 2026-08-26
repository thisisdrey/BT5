# Q5300: refund receipt to a deleted or nonexistent predecessor — ext.rs

## Question
Can an unprivileged mainnet account, entering through an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`, a receipt chain whose predecessor account is deleted before the refund is generated, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `get_receipt_receiver` in `runtime/runtime/src/ext.rs` and have the refund minted to nobody, burned twice, or credited to a recreated account, breaking the invariant that refunds are conserved: burned or credited exactly once, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/ext.rs` :: `get_receipt_receiver`
- Entrypoint: an attacker contract emitting cross-shard promises with `promise_batch_action_function_call_weight`
- Attacker controls: a receipt chain whose predecessor account is deleted before the refund is generated; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: have the refund minted to nobody, burned twice, or credited to a recreated account
- Invariant to test: refunds are conserved: burned or credited exactly once
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test deleting the predecessor before the refund lands
