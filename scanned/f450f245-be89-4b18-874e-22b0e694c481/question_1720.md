# Q1720: account storage accounting interacting with global code adoption — congestion_control.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, an account adopting and abandoning global code repeatedly within one chunk, when a referencing account is deleted while others still reference the code, reach `int_overflow_to_storage_err` in `runtime/runtime/src/congestion_control.rs` and make storage_usage drift so the account escapes storage staking entirely, breaking the invariant that storage_usage returns to its exact prior value after adopt/abandon cycles, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/congestion_control.rs` :: `int_overflow_to_storage_err`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: an account adopting and abandoning global code repeatedly within one chunk; when a referencing account is deleted while others still reference the code
- Exploit idea: make storage_usage drift so the account escapes storage staking entirely
- Invariant to test: storage_usage returns to its exact prior value after adopt/abandon cycles
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test cycling adoption and asserting storage_usage returns
