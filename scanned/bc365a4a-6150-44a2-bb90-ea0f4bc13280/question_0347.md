# Q0347: contract - unprivileged deployment claims a derived account id (3)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a receiver or callee contract the attacker deployed, invoked during a transfer callback, call `gd_deploy` in `contracts/global-deployer/src/contract.rs` to deploy or initialise at a deterministic/global-contract account id that another party's flow expects to control, so a later interaction reaches attacker code, breaking the invariant `the code deployed at a derived id == the code that id's derivation commits to, and only the intended party can claim it` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/global-deployer/src/contract.rs](contracts/global-deployer/src/contract.rs) - `gd_deploy` (cross-check `approve` in the same file)
- Entrypoint: a receiver or callee contract the attacker deployed, invoked during a transfer callback
- Attacker controls: the callee's return value, panics, and gas consumption
- Exploit idea: NEP-591/NEP-616 ids are derived from code plus initial state; front-run the honest deployment or reach an id whose derivation the attacker can influence. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the code deployed at a derived id == the code that id's derivation commits to, and only the intended party can claim it
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Race an honest `gd_deploy`/`oa_set_code` with an attacker call for the same id; assert the honest party wins or both fail.
