# Q5299: top-level account-id creation rules — transaction.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, receiver ids at the minimum and maximum length, with mixed case, leading digits, and separator-only segments, with the boundary value chosen one unit past the enforced limit, and additionally when the same input is submitted through two RPC nodes in the same block height, reach `receiver_id` in `core/primitives/src/transaction.rs` and create a name the protocol reserves, or a name that two different code paths normalise differently, breaking the invariant that account id validity and parent/child derivation agree everywhere they are evaluated, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `core/primitives/src/transaction.rs` :: `receiver_id`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: receiver ids at the minimum and maximum length, with mixed case, leading digits, and separator-only segments; with the boundary value chosen one unit past the enforced limit; when the same input is submitted through two RPC nodes in the same block height
- Exploit idea: create a name the protocol reserves, or a name that two different code paths normalise differently
- Invariant to test: account id validity and parent/child derivation agree everywhere they are evaluated
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test over AccountId parsing versus the creation-permission check
