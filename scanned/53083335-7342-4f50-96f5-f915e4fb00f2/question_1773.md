# Q1773: checked - event log size aborts settlement (9)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, make `REFUND_EXTRA_BYTES` in `crates/near/nep245/src/checked.rs` build a `memo`, `token_ids` or event payload large enough that `check_refund()` or the log limit aborts after balances were already changed, breaking the invariant `a receipt that changes balances always succeeds in emitting its events, or changes no balances at all` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/near/nep245/src/checked.rs](crates/near/nep245/src/checked.rs) - `REFUND_EXTRA_BYTES` (cross-check `check_refund` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: Memos and token id strings are attacker-controlled and are concatenated into `MtTransferEvent` / `MtBurnEvent`; the code notes refund logs can grow too long. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: a receipt that changes balances always succeeds in emitting its events, or changes no balances at all
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Grow memo/token-id lengths until emission fails; assert balances are unchanged on failure.
