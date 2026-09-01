# Q4310: deposit - DepositMessage untagged/flatten parsing picks the wrong action (8)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, craft a deposit `msg` whose JSON matches both `DepositAction::Execute` and `DepositAction::Notify` under `#[serde(untagged)]` + `#[serde(flatten)]`, so `mt_on_transfer` in `contracts/defuse/src/contract/tokens/nep245/deposit.rs` selects a different action than the depositor intended, breaking the invariant `the `DepositAction` executed == the action the depositor's serialised message unambiguously denotes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/deposit.rs](contracts/defuse/src/contract/tokens/nep245/deposit.rs) - `mt_on_transfer` (cross-check `mt_resolve_deposit` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: `untagged` tries variants in order; a message containing both `execute_intents` and `msg` fields, or neither in canonical form, resolves ambiguously. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the `DepositAction` executed == the action the depositor's serialised message unambiguously denotes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip ambiguous `DepositMessage` JSON; assert exactly one variant matches or parsing fails.
