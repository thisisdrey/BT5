# Q0555: state - unprivileged deployment claims a derived account id (3)

## Question
Given the account is registered and still holds a non-zero balance, can an unprivileged attacker, entering through a race against an honest party's deployment or initialisation call, call `OWNER_ID` in `contracts/global-deployer/src/state.rs` to deploy or initialise at a deterministic/global-contract account id that another party's flow expects to control, so a later interaction reaches attacker code, breaking the invariant `the code deployed at a derived id == the code that id's derivation commits to, and only the intended party can claim it` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/global-deployer/src/state.rs](contracts/global-deployer/src/state.rs) - `OWNER_ID` (cross-check `pre_approve_code` in the same file)
- Entrypoint: a race against an honest party's deployment or initialisation call
- Attacker controls: the timing and the arguments of the competing call
- Exploit idea: NEP-591/NEP-616 ids are derived from code plus initial state; front-run the honest deployment or reach an id whose derivation the attacker can influence. Set-up: the account is registered and still holds a non-zero balance.
- Invariant to test: the code deployed at a derived id == the code that id's derivation commits to, and only the intended party can claim it
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Race an honest `gd_deploy`/`oa_set_code` with an attacker call for the same id; assert the honest party wins or both fail.
