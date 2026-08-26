# Q5394: DeleteAccount with a non-empty state and pending receipts — adapter.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, an account holding large contract state and inbound receipts already queued from another shard, when the receiver account already exists with balance and keys, and additionally when the receiver account does not yet exist, reach `view_account` in `runtime/runtime/src/adapter.rs` and delete the account so the in-flight receipts land on a recreated or nonexistent account and their deposits vanish, breaking the invariant that no deposit in flight is lost or credited to an unrelated account when the receiver is deleted, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/adapter.rs` :: `view_account`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: an account holding large contract state and inbound receipts already queued from another shard; when the receiver account already exists with balance and keys; when the receiver account does not yet exist
- Exploit idea: delete the account so the in-flight receipts land on a recreated or nonexistent account and their deposits vanish
- Invariant to test: no deposit in flight is lost or credited to an unrelated account when the receiver is deleted
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test deleting a receiver between receipt creation and delivery
