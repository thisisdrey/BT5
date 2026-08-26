# Q3150: DeployContract mid-batch with a later FunctionCall — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through a single transaction batching many actions against an attacker-owned receiver, DeployContract followed by FunctionCall in one action list, where the new code changes storage layout, when combined with a DeployContract earlier in the same action list, and additionally when combined with a DeleteAccount later in the same action list, reach `action_deploy_global_contract` in `runtime/runtime/src/global_contracts.rs` and have the FunctionCall execute against a cached compiled artifact of the previous code, breaking the invariant that a FunctionCall always executes the code hash the account holds at that point in the batch, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `action_deploy_global_contract`
- Entrypoint: a single transaction batching many actions against an attacker-owned receiver
- Attacker controls: DeployContract followed by FunctionCall in one action list, where the new code changes storage layout; when combined with a DeployContract earlier in the same action list; when combined with a DeleteAccount later in the same action list
- Exploit idea: have the FunctionCall execute against a cached compiled artifact of the previous code
- Invariant to test: a FunctionCall always executes the code hash the account holds at that point in the batch
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: runtime test asserting the executed code hash after an in-batch redeploy
