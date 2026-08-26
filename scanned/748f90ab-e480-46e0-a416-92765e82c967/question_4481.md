# Q4481: global contract code interaction with the compiled artifact cache — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, the same code deployed both as a global contract and as a normal contract, when two account-creation paths race for the same id in one block, and additionally when links are saturated across the exact resharding block, reach `create_deterministic_account` in `runtime/runtime/src/deterministic_account_id.rs` and make one form execute the other's cached artifact under a different gas schedule, breaking the invariant that cache keys distinguish deployment form when it affects semantics or cost, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/deterministic_account_id.rs` :: `create_deterministic_account`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: the same code deployed both as a global contract and as a normal contract; when two account-creation paths race for the same id in one block; when links are saturated across the exact resharding block
- Exploit idea: make one form execute the other's cached artifact under a different gas schedule
- Invariant to test: cache keys distinguish deployment form when it affects semantics or cost
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test deploying identical code both ways and comparing execution
