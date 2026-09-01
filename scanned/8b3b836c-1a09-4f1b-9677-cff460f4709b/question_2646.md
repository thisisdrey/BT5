# Q2646: core - token_id re-parse panic inside a private callback (6)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled, supply a `token_id` string at the entrypoint that `mt_batch_transfer` in `contracts/defuse/src/contract/tokens/nep245/core.rs` re-parses with `unwrap_or_else(|e| panic!(...))`, so the callback aborts and the transferred balance is stranded, breaking the invariant `every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/nep245/core.rs](contracts/defuse/src/contract/tokens/nep245/core.rs) - `mt_batch_transfer` (cross-check `mt_resolve_gas` in the same file)
- Entrypoint: `ft_withdraw` / `nft_withdraw` / `mt_withdraw` called directly by an account with `auth_by_predecessor_id` enabled
- Attacker controls: `token`, `receiver_id`, `amount`, `memo`, `msg`, `storage_deposit` and `min_gas`
- Exploit idea: `mt_resolve_transfer` re-parses `token_ids` from strings that originated in caller-supplied arguments; a value that passes the outbound path but fails the inbound parse freezes the refund. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Fuzz `mt_batch_transfer_call` token ids; assert every accepted id parses in the resolver.
