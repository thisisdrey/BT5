# Q5656: global contract code interaction with the compiled artifact cache — deterministic_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, the same code deployed both as a global contract and as a normal contract, when links are saturated across the exact resharding block, and additionally when the interaction crosses a protocol-version upgrade with receipts in flight, reach `version` in `core/primitives-core/src/deterministic_account_id.rs` and make one form execute the other's cached artifact under a different gas schedule, breaking the invariant that cache keys distinguish deployment form when it affects semantics or cost, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives-core/src/deterministic_account_id.rs` :: `version`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: the same code deployed both as a global contract and as a normal contract; when links are saturated across the exact resharding block; when the interaction crosses a protocol-version upgrade with receipts in flight
- Exploit idea: make one form execute the other's cached artifact under a different gas schedule
- Invariant to test: cache keys distinguish deployment form when it affects semantics or cost
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: unit test deploying identical code both ways and comparing execution
