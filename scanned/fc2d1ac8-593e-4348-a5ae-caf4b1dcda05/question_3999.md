# Q3999: core - refund from a locked account bypasses lock semantics (2)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, use the documented allowance in `internal_mt_batch_transfer` of `contracts/defuse/src/contract/tokens/nep245/core.rs` that refunds may move funds out of a locked receiver, to extract value from an account the protocol intended to freeze, breaking the invariant `assets leaving a locked account == refunds of transfers that account itself received in the same flow` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/core.rs](contracts/defuse/src/contract/tokens/nep245/core.rs) - `internal_mt_batch_transfer` (cross-check `mt_transfer_call` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: The resolver deliberately uses `as_inner_unchecked_mut()`; probe whether an attacker can lock/trigger the sequence to drain a frozen account, or make a lock ineffective. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: assets leaving a locked account == refunds of transfers that account itself received in the same flow
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Lock a receiver between transfer and resolve; assert only the in-flight amount can leave.
