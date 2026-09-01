# Q5448: withdraw - event log size aborts settlement (3)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, make `do_mt_withdraw` in `contracts/defuse/src/contract/tokens/nep245/withdraw.rs` build a `memo`, `token_ids` or event payload large enough that `check_refund()` or the log limit aborts after balances were already changed, breaking the invariant `a receipt that changes balances always succeeds in emitting its events, or changes no balances at all` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/withdraw.rs](contracts/defuse/src/contract/tokens/nep245/withdraw.rs) - `do_mt_withdraw` (cross-check `DO_MT_WITHDRAW_GAS` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: Memos and token id strings are attacker-controlled and are concatenated into `MtTransferEvent` / `MtBurnEvent`; the code notes refund logs can grow too long. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: a receipt that changes balances always succeeds in emitting its events, or changes no balances at all
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Grow memo/token-id lengths until emission fails; assert balances are unchanged on failure.
