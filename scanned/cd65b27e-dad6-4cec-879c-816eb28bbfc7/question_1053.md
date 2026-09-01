# Q1053: withdraw - failed promise treated as fully used (no refund)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, make the promise feeding `FT_RESOLVE_WITHDRAW_GAS` in `contracts/defuse/src/contract/tokens/nep141/withdraw.rs` fail in a way the resolver classifies as 'used', so the debited balance is neither delivered nor returned, breaking the invariant `for every withdrawal, assets delivered + assets refunded == assets debited` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep141/withdraw.rs](contracts/defuse/src/contract/tokens/nep141/withdraw.rs) - `FT_RESOLVE_WITHDRAW_GAS` (cross-check `ft_withdraw` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: `ft_resolve_withdraw` sets `used = amount` on any promise error when `is_call`, deliberately not refunding; probe whether an attacker can force that error path for a victim's withdrawal. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: for every withdrawal, assets delivered + assets refunded == assets debited
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Force the callee to fail after the resolver's gas budget is exhausted; assert whether the owner's balance is restored.
