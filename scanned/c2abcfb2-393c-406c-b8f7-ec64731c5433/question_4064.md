# Q4064: top-level account-id creation rules — config.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, receiver ids at the minimum and maximum length, with mixed case, leading digits, and separator-only segments, with the boundary value chosen exactly at the enforced limit, and additionally with the boundary value chosen one unit past the enforced limit, reach `delegate_inner_action` in `runtime/runtime/src/config.rs` and create a name the protocol reserves, or a name that two different code paths normalise differently, breaking the invariant that account id validity and parent/child derivation agree everywhere they are evaluated, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/config.rs` :: `delegate_inner_action`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: receiver ids at the minimum and maximum length, with mixed case, leading digits, and separator-only segments; with the boundary value chosen exactly at the enforced limit; with the boundary value chosen one unit past the enforced limit
- Exploit idea: create a name the protocol reserves, or a name that two different code paths normalise differently
- Invariant to test: account id validity and parent/child derivation agree everywhere they are evaluated
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test over AccountId parsing versus the creation-permission check
