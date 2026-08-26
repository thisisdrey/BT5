# Q0078: interaction between global contracts and account deletion — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, an account using a global contract, deleted while other accounts still reference the code, when a referencing account is deleted while others still reference the code, reach `create_deterministic_account` in `runtime/runtime/src/deterministic_account_id.rs` and leave code referenced by nobody but still charged, or referenced by accounts whose code is gone, breaking the invariant that code lifetime and reference accounting survive every account lifecycle event, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `runtime/runtime/src/deterministic_account_id.rs` :: `create_deterministic_account`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: an account using a global contract, deleted while other accounts still reference the code; when a referencing account is deleted while others still reference the code
- Exploit idea: leave code referenced by nobody but still charged, or referenced by accounts whose code is gone
- Invariant to test: code lifetime and reference accounting survive every account lifecycle event
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: runtime test deleting a user of a global contract and checking the code state
