# Q2471: top-level account-id creation rules — access_keys.rs

## Question
Can an unprivileged mainnet account, entering through `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts, receiver ids at the minimum and maximum length, with mixed case, leading digits, and separator-only segments, with the boundary value chosen exactly at the enforced limit, reach `action_add_key` in `runtime/runtime/src/access_keys.rs` and create a name the protocol reserves, or a name that two different code paths normalise differently, breaking the invariant that account id validity and parent/child derivation agree everywhere they are evaluated, leading to Critical - Unintended permanent chain split requiring a hard fork?

## Target
- File/function: `runtime/runtime/src/access_keys.rs` :: `action_add_key`
- Entrypoint: `CreateAccount` + `DeleteAccount` action sequences on attacker-owned sub-accounts
- Attacker controls: receiver ids at the minimum and maximum length, with mixed case, leading digits, and separator-only segments; with the boundary value chosen exactly at the enforced limit
- Exploit idea: create a name the protocol reserves, or a name that two different code paths normalise differently
- Invariant to test: account id validity and parent/child derivation agree everywhere they are evaluated
- Expected Immunefi impact: Critical - Unintended permanent chain split requiring a hard fork
- Fast validation: differential test over AccountId parsing versus the creation-permission check
