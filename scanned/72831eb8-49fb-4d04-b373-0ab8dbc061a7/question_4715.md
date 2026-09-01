# Q4715: auth - state_init (NEP-616) deployment changes the callee identity (7)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, use the `state_init` field reached through `min_gas` in `contracts/defuse/core/src/intents/auth.rs` to deploy code at the deterministic account id right before the call, so the contract that receives `on_auth`/`mt_on_transfer` is not the one the signer expected, breaking the invariant `the code executing at the `state_init` target == the code the derived `AccountId` commits to` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/intents/auth.rs](contracts/defuse/core/src/intents/auth.rs) - `min_gas` (cross-check `MIN_GAS_DEFAULT` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: The derived id commits to the global contract id plus initial storage; probe whether the same id can be reached with different code, or whether an existing account is silently reused. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the code executing at the `state_init` target == the code the derived `AccountId` commits to
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Derive an id, deploy independently, then send a `state_init` call; assert the pre-existing code is not silently used.
