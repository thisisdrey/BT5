# Q5485: DeleteAccount beneficiary loop — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, two attacker accounts each naming the other as DeleteAccount beneficiary in the same block, when the receiver account already exists with balance and keys, and additionally when the receiver account does not yet exist, reach `append_action_delete_account` in `runtime/runtime/src/receipt_manager.rs` and make the transfer of remaining balance loop, drop, or double-credit, breaking the invariant that the deleted account's balance lands exactly once with the beneficiary or is refunded, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `append_action_delete_account`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: two attacker accounts each naming the other as DeleteAccount beneficiary in the same block; when the receiver account already exists with balance and keys; when the receiver account does not yet exist
- Exploit idea: make the transfer of remaining balance loop, drop, or double-credit
- Invariant to test: the deleted account's balance lands exactly once with the beneficiary or is refunded
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test applying reciprocal DeleteAccount actions in one chunk
