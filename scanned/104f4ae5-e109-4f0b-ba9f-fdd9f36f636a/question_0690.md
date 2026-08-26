# Q0690: global contract code interaction with the compiled artifact cache — lib.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, the same code deployed both as a global contract and as a normal contract, when a referencing account is deleted while others still reference the code, reach the primary handler in this file in `runtime/runtime/src/lib.rs` and make one form execute the other's cached artifact under a different gas schedule, breaking the invariant that cache keys distinguish deployment form when it affects semantics or cost, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/lib.rs` :: primary handler
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: the same code deployed both as a global contract and as a normal contract; when a referencing account is deleted while others still reference the code
- Exploit idea: make one form execute the other's cached artifact under a different gas schedule
- Invariant to test: cache keys distinguish deployment form when it affects semantics or cost
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test deploying identical code both ways and comparing execution
