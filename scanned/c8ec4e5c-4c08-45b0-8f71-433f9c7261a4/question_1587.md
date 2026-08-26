# Q1587: DeterministicStateInit onto an existing account — account.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, an init targeting an account id that already exists with balance and keys, when combined with a DeployContract earlier in the same action list, reach `uninitialized_account_from_serde` in `core/primitives-core/src/account.rs` and overwrite or merge into existing state so the original owner loses control or balance, breaking the invariant that deterministic state init only ever initialises an account that does not yet exist, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `core/primitives-core/src/account.rs` :: `uninitialized_account_from_serde`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: an init targeting an account id that already exists with balance and keys; when combined with a DeployContract earlier in the same action list
- Exploit idea: overwrite or merge into existing state so the original owner loses control or balance
- Invariant to test: deterministic state init only ever initialises an account that does not yet exist
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test applying init against a pre-existing account and asserting rejection
