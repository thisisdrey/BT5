# Q4353: account recreation after deletion in the same chunk — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, DeleteAccount then CreateAccount for the same id inside one action list, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `check_and_update_nonce` in `runtime/runtime/src/global_contracts.rs` and resurrect the id with stale trie keys still present under the old account's prefix, breaking the invariant that creating an account id leaves no reachable state from a previous incarnation, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `check_and_update_nonce`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: DeleteAccount then CreateAccount for the same id inside one action list; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: resurrect the id with stale trie keys still present under the old account's prefix
- Invariant to test: creating an account id leaves no reachable state from a previous incarnation
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: trie inspection test after delete+create of one id
