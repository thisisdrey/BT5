# Q4950: deposit - event log size aborts settlement

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, make `mt_resolve_deposit` in `contracts/defuse/src/contract/tokens/nep245/deposit.rs` build a `memo`, `token_ids` or event payload large enough that `check_refund()` or the log limit aborts after balances were already changed, breaking the invariant `a receipt that changes balances always succeeds in emitting its events, or changes no balances at all` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/deposit.rs](contracts/defuse/src/contract/tokens/nep245/deposit.rs) - `mt_resolve_deposit` (cross-check `mt_on_transfer` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: Memos and token id strings are attacker-controlled and are concatenated into `MtTransferEvent` / `MtBurnEvent`; the code notes refund logs can grow too long. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: a receipt that changes balances always succeeds in emitting its events, or changes no balances at all
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Grow memo/token-id lengths until emission fails; assert balances are unchanged on failure.
