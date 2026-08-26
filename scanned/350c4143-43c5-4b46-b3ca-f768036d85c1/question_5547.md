# Q5547: account recreation after deletion in the same chunk — receipt_manager.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, DeleteAccount then CreateAccount for the same id inside one action list, when the receiver account already exists with balance and keys, and additionally when the receiver account does not yet exist, reach `get_receipt_receiver` in `runtime/runtime/src/receipt_manager.rs` and resurrect the id with stale trie keys still present under the old account's prefix, breaking the invariant that creating an account id leaves no reachable state from a previous incarnation, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/receipt_manager.rs` :: `get_receipt_receiver`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: DeleteAccount then CreateAccount for the same id inside one action list; when the receiver account already exists with balance and keys; when the receiver account does not yet exist
- Exploit idea: resurrect the id with stale trie keys still present under the old account's prefix
- Invariant to test: creating an account id leaves no reachable state from a previous incarnation
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: trie inspection test after delete+create of one id
