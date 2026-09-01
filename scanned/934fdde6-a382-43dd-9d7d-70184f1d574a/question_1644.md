# Q1644: withdraw - failed promise treated as fully used (no refund) (12)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, make the promise feeding `internal_mt_withdraw` in `contracts/defuse/src/contract/tokens/nep245/withdraw.rs` fail in a way the resolver classifies as 'used', so the debited balance is neither delivered nor returned, breaking the invariant `for every withdrawal, assets delivered + assets refunded == assets debited` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/withdraw.rs](contracts/defuse/src/contract/tokens/nep245/withdraw.rs) - `internal_mt_withdraw` (cross-check `MT_RESOLVE_WITHDRAW_BASE_GAS` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: `ft_resolve_withdraw` sets `used = amount` on any promise error when `is_call`, deliberately not refunding; probe whether an attacker can force that error path for a victim's withdrawal. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: for every withdrawal, assets delivered + assets refunded == assets debited
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Force the callee to fail after the resolver's gas budget is exhausted; assert whether the owner's balance is restored.
