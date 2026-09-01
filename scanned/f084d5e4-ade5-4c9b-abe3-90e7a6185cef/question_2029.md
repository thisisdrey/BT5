# Q2029: deposit - refund routed to the wrong owner in a batch (2)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, exploit that `nft_resolve_deposit` in `contracts/defuse/src/contract/tokens/nep171/deposit.rs` derives the refund recipient from `previous_owner_ids.first()` (or an equivalent single-sender assumption) so a multi-owner batch refunds one party's assets to another, breaking the invariant `the account credited by a refund == the account that was debited for that exact token and amount` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep171/deposit.rs](contracts/defuse/src/contract/tokens/nep171/deposit.rs) - `nft_resolve_deposit` (cross-check `nft_on_transfer` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: The `require!(sender_id == previous_owner_id)` guard rejects approvals, but probe every path that constructs `previous_owner_ids` and whether a mismatch is reachable. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the account credited by a refund == the account that was debited for that exact token and amount
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Construct a batch with differing `previous_owner_ids`; assert the resolver rejects rather than misrouting.
