# Q3998: withdraw - refund routed to the wrong owner in a batch (4)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, exploit that `do_nft_withdraw` in `contracts/defuse/src/contract/tokens/nep171/withdraw.rs` derives the refund recipient from `previous_owner_ids.first()` (or an equivalent single-sender assumption) so a multi-owner batch refunds one party's assets to another, breaking the invariant `the account credited by a refund == the account that was debited for that exact token and amount` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep171/withdraw.rs](contracts/defuse/src/contract/tokens/nep171/withdraw.rs) - `do_nft_withdraw` (cross-check `NFT_RESOLVE_WITHDRAW_GAS` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: The `require!(sender_id == previous_owner_id)` guard rejects approvals, but probe every path that constructs `previous_owner_ids` and whether a mismatch is reachable. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: the account credited by a refund == the account that was debited for that exact token and amount
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Construct a batch with differing `previous_owner_ids`; assert the resolver rejects rather than misrouting.
