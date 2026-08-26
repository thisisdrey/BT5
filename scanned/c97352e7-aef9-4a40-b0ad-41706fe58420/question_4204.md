# Q4204: interaction between global contracts and account deletion — manager.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, an account using a global contract, deleted while other accounts still reference the code, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `process_memtrie_resharding_storage_update` in `chain/chain/src/resharding/manager.rs` and leave code referenced by nobody but still charged, or referenced by accounts whose code is gone, breaking the invariant that code lifetime and reference accounting survive every account lifecycle event, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `chain/chain/src/resharding/manager.rs` :: `process_memtrie_resharding_storage_update`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: an account using a global contract, deleted while other accounts still reference the code; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: leave code referenced by nobody but still charged, or referenced by accounts whose code is gone
- Invariant to test: code lifetime and reference accounting survive every account lifecycle event
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: runtime test deleting a user of a global contract and checking the code state
