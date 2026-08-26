# Q5996: DeterministicStateInit onto an existing account — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, an init targeting an account id that already exists with balance and keys, when the receiver account already exists with balance and keys, and additionally when the receiver account does not yet exist, reach `action_deploy_global_contract` in `runtime/runtime/src/global_contracts.rs` and overwrite or merge into existing state so the original owner loses control or balance, breaking the invariant that deterministic state init only ever initialises an account that does not yet exist, leading to High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `action_deploy_global_contract`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: an init targeting an account id that already exists with balance and keys; when the receiver account already exists with balance and keys; when the receiver account does not yet exist
- Exploit idea: overwrite or merge into existing state so the original owner loses control or balance
- Invariant to test: deterministic state init only ever initialises an account that does not yet exist
- Expected Immunefi impact: High - Direct theft of user funds via access-key / meta-transaction / wallet-contract authorization bypass
- Fast validation: runtime test applying init against a pre-existing account and asserting rejection
