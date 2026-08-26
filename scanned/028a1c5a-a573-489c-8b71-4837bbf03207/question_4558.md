# Q4558: DeployContract mid-batch with a later FunctionCall — receipt.rs

## Question
Can an unprivileged mainnet account, entering through a single transaction batching many actions against an attacker-owned receiver, DeployContract followed by FunctionCall in one action list, where the new code changes storage layout, when combined with a DeleteAccount later in the same action list, and additionally when the receiver account already exists with balance and keys, reach `balance_refund_receiver` in `core/primitives/src/receipt.rs` and have the FunctionCall execute against a cached compiled artifact of the previous code, breaking the invariant that a FunctionCall always executes the code hash the account holds at that point in the batch, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/receipt.rs` :: `balance_refund_receiver`
- Entrypoint: a single transaction batching many actions against an attacker-owned receiver
- Attacker controls: DeployContract followed by FunctionCall in one action list, where the new code changes storage layout; when combined with a DeleteAccount later in the same action list; when the receiver account already exists with balance and keys
- Exploit idea: have the FunctionCall execute against a cached compiled artifact of the previous code
- Invariant to test: a FunctionCall always executes the code hash the account holds at that point in the batch
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: runtime test asserting the executed code hash after an in-batch redeploy
