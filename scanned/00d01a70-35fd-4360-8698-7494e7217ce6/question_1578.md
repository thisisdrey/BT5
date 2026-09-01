# Q1578: checked - event log size aborts settlement (8)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, make `REFUND_STR_LEN` in `crates/near/nep245/src/checked.rs` build a `memo`, `token_ids` or event payload large enough that `check_refund()` or the log limit aborts after balances were already changed, breaking the invariant `a receipt that changes balances always succeeds in emitting its events, or changes no balances at all` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/near/nep245/src/checked.rs](crates/near/nep245/src/checked.rs) - `REFUND_STR_LEN` (cross-check `savings` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: Memos and token id strings are attacker-controlled and are concatenated into `MtTransferEvent` / `MtBurnEvent`; the code notes refund logs can grow too long. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: a receipt that changes balances always succeeds in emitting its events, or changes no balances at all
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Grow memo/token-id lengths until emission fails; assert balances are unchanged on failure.
