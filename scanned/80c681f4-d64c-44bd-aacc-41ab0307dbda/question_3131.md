# Q3131: mod - token_id re-parse panic inside a private callback (5)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote, supply a `token_id` string at the entrypoint that `mt_resolve_deposit_gas` in `contracts/defuse/src/contract/tokens/mod.rs` re-parses with `unwrap_or_else(|e| panic!(...))`, so the callback aborts and the transferred balance is stranded, breaking the invariant `every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/tokens/mod.rs](contracts/defuse/src/contract/tokens/mod.rs) - `mt_resolve_deposit_gas` (cross-check `resolve_deposit_internal` in the same file)
- Entrypoint: `ft_on_transfer` / `nft_on_transfer` / `mt_on_transfer` from a token contract the attacker wrote
- Attacker controls: `sender_id`, `amount`, the `msg` (receiver, notify, or nested intents), and the token's own behaviour
- Exploit idea: `mt_resolve_transfer` re-parses `token_ids` from strings that originated in caller-supplied arguments; a value that passes the outbound path but fails the inbound parse freezes the refund. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Fuzz `mt_batch_transfer_call` token ids; assert every accepted id parses in the resolver.
