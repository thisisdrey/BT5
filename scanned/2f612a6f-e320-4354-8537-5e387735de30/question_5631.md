# Q5631: mod - DepositMessage untagged/flatten parsing picks the wrong action (14)

## Question
Given the withdrawal carries `storage_deposit: Some(..)` funded from the signer's wNEAR balance, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, craft a deposit `msg` whose JSON matches both `DepositAction::Execute` and `DepositAction::Notify` under `#[serde(untagged)]` + `#[serde(flatten)]`, so `DepositAction` in `contracts/defuse/src/tokens/mod.rs` selects a different action than the depositor intended, breaking the invariant `the `DepositAction` executed == the action the depositor's serialised message unambiguously denotes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/tokens/mod.rs](contracts/defuse/src/tokens/mod.rs) - `DepositAction` (cross-check `with_action` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: `untagged` tries variants in order; a message containing both `execute_intents` and `msg` fields, or neither in canonical form, resolves ambiguously. Set-up: the withdrawal carries `storage_deposit: Some(..)` funded from the signer's wNEAR balance.
- Invariant to test: the `DepositAction` executed == the action the depositor's serialised message unambiguously denotes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip ambiguous `DepositMessage` JSON; assert exactly one variant matches or parsing fails.
