# Q4759: resolver - event log size aborts settlement (4)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, make `mt_resolve_transfer` in `contracts/defuse/src/contract/tokens/nep245/resolver.rs` build a `memo`, `token_ids` or event payload large enough that `check_refund()` or the log limit aborts after balances were already changed, breaking the invariant `a receipt that changes balances always succeeds in emitting its events, or changes no balances at all` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/resolver.rs](contracts/defuse/src/contract/tokens/nep245/resolver.rs) - `mt_resolve_transfer`
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: Memos and token id strings are attacker-controlled and are concatenated into `MtTransferEvent` / `MtBurnEvent`; the code notes refund logs can grow too long. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: a receipt that changes balances always succeeds in emitting its events, or changes no balances at all
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Grow memo/token-id lengths until emission fails; assert balances are unchanged on failure.
