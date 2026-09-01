# Q2773: mod - DepositMessage untagged/flatten parsing picks the wrong action (4)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, craft a deposit `msg` whose JSON matches both `DepositAction::Execute` and `DepositAction::Notify` under `#[serde(untagged)]` + `#[serde(flatten)]`, so `DepositAction` in `contracts/defuse/src/tokens/mod.rs` selects a different action than the depositor intended, breaking the invariant `the `DepositAction` executed == the action the depositor's serialised message unambiguously denotes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/tokens/mod.rs](contracts/defuse/src/tokens/mod.rs) - `DepositAction` (cross-check `ParseDepositMessageError` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: `untagged` tries variants in order; a message containing both `execute_intents` and `msg` fields, or neither in canonical form, resolves ambiguously. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the `DepositAction` executed == the action the depositor's serialised message unambiguously denotes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip ambiguous `DepositMessage` JSON; assert exactly one variant matches or parsing fails.
