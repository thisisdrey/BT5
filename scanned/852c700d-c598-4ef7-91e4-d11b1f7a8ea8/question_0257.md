# Q0257: withdraw - attacker-controlled promise result inflates the refund (6)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, return a crafted value from an attacker-deployed token or receiver contract so `mt_resolve_withdraw` in `contracts/defuse/src/contract/tokens/nep245/withdraw.rs` re-credits more than the amount that actually failed to settle, breaking the invariant `value debited == value actually delivered + value re-credited by the resolver` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/withdraw.rs](contracts/defuse/src/contract/tokens/nep245/withdraw.rs) - `mt_resolve_withdraw` (cross-check `do_mt_withdraw` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: The refund is parsed from the callee's return value with `promise_result_checked_json*`; drive the parsed value above the amount actually retained by the callee, or make the clamp (`min(amount)`, `min(balance_left)`) select the wrong bound. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: value debited == value actually delivered + value re-credited by the resolver
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy a token whose `ft_transfer_call` transfers everything but returns `0`; assert the balance is not re-credited.
