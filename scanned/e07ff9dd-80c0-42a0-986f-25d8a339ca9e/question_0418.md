# Q0418: account recreation after deletion in the same chunk — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, DeleteAccount then CreateAccount for the same id inside one action list, when combined with a DeployContract earlier in the same action list, reach `action_use_global_contract` in `runtime/runtime/src/global_contracts.rs` and resurrect the id with stale trie keys still present under the old account's prefix, breaking the invariant that creating an account id leaves no reachable state from a previous incarnation, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `action_use_global_contract`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: DeleteAccount then CreateAccount for the same id inside one action list; when combined with a DeployContract earlier in the same action list
- Exploit idea: resurrect the id with stale trie keys still present under the old account's prefix
- Invariant to test: creating an account id leaves no reachable state from a previous incarnation
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: trie inspection test after delete+create of one id
