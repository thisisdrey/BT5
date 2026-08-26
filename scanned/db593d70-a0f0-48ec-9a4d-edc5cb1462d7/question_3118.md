# Q3118: universal state init combined with a global contract reference — universal_account_id.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, an init whose state references a global contract withdrawn in the same block, when a referencing account is deleted while others still reference the code, and additionally when two account-creation paths race for the same id in one block, reach `is_universal_account_id` in `core/primitives-core/src/universal_account_id.rs` and create an account permanently pointing at code that no longer exists, breaking the invariant that an account can never be initialised against unresolvable code, leading to Critical - Permanent freezing of funds (fix requires hardfork)?

## Target
- File/function: `core/primitives-core/src/universal_account_id.rs` :: `is_universal_account_id`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: an init whose state references a global contract withdrawn in the same block; when a referencing account is deleted while others still reference the code; when two account-creation paths race for the same id in one block
- Exploit idea: create an account permanently pointing at code that no longer exists
- Invariant to test: an account can never be initialised against unresolvable code
- Expected Immunefi impact: Critical - Permanent freezing of funds (fix requires hardfork)
- Fast validation: runtime test initialising against a code reference removed in the same chunk
