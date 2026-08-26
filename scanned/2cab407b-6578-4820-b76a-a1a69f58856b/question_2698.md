# Q2698: interaction between global contracts and account deletion — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, an account using a global contract, deleted while other accounts still reference the code, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `data_mut` in `core/primitives-core/src/deterministic_account_id.rs` and leave code referenced by nobody but still charged, or referenced by accounts whose code is gone, breaking the invariant that code lifetime and reference accounting survive every account lifecycle event, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/primitives-core/src/deterministic_account_id.rs` :: `data_mut`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: an account using a global contract, deleted while other accounts still reference the code; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: leave code referenced by nobody but still charged, or referenced by accounts whose code is gone
- Invariant to test: code lifetime and reference accounting survive every account lifecycle event
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: runtime test deleting a user of a global contract and checking the code state
