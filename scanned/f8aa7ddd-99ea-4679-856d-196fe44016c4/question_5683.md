# Q5683: storage_deposit - re-entrancy through an attacker-named callee (17)

## Question
Given the token contract is one the attacker deployed and can fail on demand, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, have the contract named in `msg` / `AuthCall::contract_id` / `NotifyOnTransfer` re-enter `execute_intents`, `ft_withdraw` or a `*_resolve_*` path while `DO_STORAGE_DEPOSIT_GAS` in `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs` is mid-settlement, observing or mutating state between the debit and the credit, breaking the invariant `the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs](contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs) - `DO_STORAGE_DEPOSIT_GAS` (cross-check `do_storage_deposit` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: `Engine::finalize` has already run when the promise fires; the callee can spend the same balance again or take goods plus refund. Set-up: the token contract is one the attacker deployed and can fail on demand.
- Invariant to test: the balance changes committed by a receipt == the balance changes the signed intents authorise, whatever any callee does
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy an `on_auth`/`mt_on_transfer` that calls back into `execute_intents`; assert no balance is spent twice.
