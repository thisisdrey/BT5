# Q0667: withdraw - attacker-controlled promise result inflates the refund (12)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, return a crafted value from an attacker-deployed token or receiver contract so `internal_mt_withdraw` in `contracts/defuse/src/contract/tokens/nep245/withdraw.rs` re-credits more than the amount that actually failed to settle, breaking the invariant `value debited == value actually delivered + value re-credited by the resolver` and leading to direct theft of user funds: double settlement (assets delivered AND re-credited)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/withdraw.rs](contracts/defuse/src/contract/tokens/nep245/withdraw.rs) - `internal_mt_withdraw` (cross-check `DO_MT_WITHDRAW_GAS` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: The refund is parsed from the callee's return value with `promise_result_checked_json*`; drive the parsed value above the amount actually retained by the callee, or make the clamp (`min(amount)`, `min(balance_left)`) select the wrong bound. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: value debited == value actually delivered + value re-credited by the resolver
- Expected Immunefi impact: Critical - Direct theft of user funds: double settlement (assets delivered AND re-credited)
- Fast validation: Sandbox: deploy a token whose `ft_transfer_call` transfers everything but returns `0`; assert the balance is not re-credited.
