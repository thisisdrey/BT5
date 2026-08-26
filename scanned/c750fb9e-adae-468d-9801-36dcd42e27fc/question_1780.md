# Q1780: UniversalStateInit action state/deposit accounting — global_contracts.rs

## Question
Can an unprivileged mainnet account, entering through a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account, an init carrying maximal state entries and a deposit exactly at the storage-staking floor, when combined with a DeployContract earlier in the same action list, reach `action_deploy_global_contract` in `runtime/runtime/src/global_contracts.rs` and create an account whose storage cost was never charged, or whose deposit was charged twice, breaking the invariant that state written by an init is fully paid for by the deposit attached to that same action, leading to Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)?

## Target
- File/function: `runtime/runtime/src/global_contracts.rs` :: `action_deploy_global_contract`
- Entrypoint: a `DeterministicStateInit` / `UniversalStateInit` action creating an attacker-controlled account
- Attacker controls: an init carrying maximal state entries and a deposit exactly at the storage-staking floor; when combined with a DeployContract earlier in the same action list
- Exploit idea: create an account whose storage cost was never charged, or whose deposit was charged twice
- Invariant to test: state written by an init is fully paid for by the deposit attached to that same action
- Expected Immunefi impact: Critical - Direct loss of funds (protocol-level balance created, destroyed, or moved outside protocol rules)
- Fast validation: runtime test comparing storage_usage against the charged deposit after init
