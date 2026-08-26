# Q1034: global contract deploy/use lifecycle — receipt.rs

## Question
Can an unprivileged mainnet account, entering through a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account, a global contract deployed by account-id reference, then redeployed with different code under the same id, when combined with a DeployContract earlier in the same action list, reach `new_global_contract_distribution` in `core/primitives/src/receipt.rs` and make a `UseGlobalContract` account silently switch to different code, or keep executing withdrawn code, breaking the invariant that an account using a global contract executes exactly the code the reference resolves to at execution time, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `new_global_contract_distribution`
- Entrypoint: a `DeployGlobalContract` action followed by `UseGlobalContract` from a second attacker account
- Attacker controls: a global contract deployed by account-id reference, then redeployed with different code under the same id; when combined with a DeployContract earlier in the same action list
- Exploit idea: make a `UseGlobalContract` account silently switch to different code, or keep executing withdrawn code
- Invariant to test: an account using a global contract executes exactly the code the reference resolves to at execution time
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: runtime test redeploying a by-account-id global contract between two calls
