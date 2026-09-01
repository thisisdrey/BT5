# Q4815: deposit - refund routed to the wrong owner in a batch (19)

## Question
Given the receiver accepts the assets and then panics, can an unprivileged attacker, entering through the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback, exploit that `ft_resolve_deposit` in `contracts/defuse/src/contract/tokens/nep141/deposit.rs` derives the refund recipient from `previous_owner_ids.first()` (or an equivalent single-sender assumption) so a multi-owner batch refunds one party's assets to another, breaking the invariant `the account credited by a refund == the account that was debited for that exact token and amount` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep141/deposit.rs](contracts/defuse/src/contract/tokens/nep141/deposit.rs) - `ft_resolve_deposit` (cross-check `ft_on_transfer` in the same file)
- Entrypoint: the return value of the attacker's own FT/MT/receiver contract, fed into the `#[private]` resolve callback
- Attacker controls: the exact JSON the callee returns, whether it panics, and how much gas it burns
- Exploit idea: The `require!(sender_id == previous_owner_id)` guard rejects approvals, but probe every path that constructs `previous_owner_ids` and whether a mismatch is reachable. Set-up: the receiver accepts the assets and then panics.
- Invariant to test: the account credited by a refund == the account that was debited for that exact token and amount
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Construct a batch with differing `previous_owner_ids`; assert the resolver rejects rather than misrouting.
