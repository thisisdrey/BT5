# Q4824: withdraw - refund routed to the wrong owner in a batch (13)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, exploit that `do_mt_withdraw` in `contracts/defuse/src/contract/tokens/nep245/withdraw.rs` derives the refund recipient from `previous_owner_ids.first()` (or an equivalent single-sender assumption) so a multi-owner batch refunds one party's assets to another, breaking the invariant `the account credited by a refund == the account that was debited for that exact token and amount` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/withdraw.rs](contracts/defuse/src/contract/tokens/nep245/withdraw.rs) - `do_mt_withdraw` (cross-check `internal_mt_withdraw` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: The `require!(sender_id == previous_owner_id)` guard rejects approvals, but probe every path that constructs `previous_owner_ids` and whether a mismatch is reachable. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the account credited by a refund == the account that was debited for that exact token and amount
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Construct a batch with differing `previous_owner_ids`; assert the resolver rejects rather than misrouting.
