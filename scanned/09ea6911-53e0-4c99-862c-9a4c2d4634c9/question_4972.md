# Q4972: account storage accounting interacting with global code adoption — memtries.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, an account adopting and abandoning global code repeatedly within one chunk, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `new_from_arena_and_root` in `core/store/src/trie/mem/memtries.rs` and make storage_usage drift so the account escapes storage staking entirely, breaking the invariant that storage_usage returns to its exact prior value after adopt/abandon cycles, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `core/store/src/trie/mem/memtries.rs` :: `new_from_arena_and_root`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: an account adopting and abandoning global code repeatedly within one chunk; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: make storage_usage drift so the account escapes storage staking entirely
- Invariant to test: storage_usage returns to its exact prior value after adopt/abandon cycles
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test cycling adoption and asserting storage_usage returns
