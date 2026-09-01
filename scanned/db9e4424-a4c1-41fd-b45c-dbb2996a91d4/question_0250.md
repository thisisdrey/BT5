# Q0250: storage_deposit - gas starvation of the resolver callback (2)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, choose `min_gas` / `state_init` / batch size at the entrypoint so the `do_storage_deposit` callback in `contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs` runs out of gas after the debit, leaving the balance burned with no refund, breaking the invariant `the resolver callback always executes with enough gas to restore the debited balance` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs](contracts/defuse/src/contract/tokens/nep141/storage_deposit.rs) - `do_storage_deposit` (cross-check `DO_STORAGE_DEPOSIT_GAS` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: The floors (`FT_TRANSFER_CALL_GAS_MIN`, `MT_BATCH_TRANSFER_GAS_MIN`, `AuthCall::MIN_GAS_DEFAULT`, `STATE_INIT_GAS`) plus `with_unused_gas_weight(0)` are the only protection; probe whether a caller-chosen value passes the floor but starves the resolve step. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the resolver callback always executes with enough gas to restore the debited balance
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Sweep `min_gas` and token-count values; assert the resolver completes for every value the entrypoint accepts.
