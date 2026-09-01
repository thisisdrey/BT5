# Q3497: state - token_id re-parse panic inside a private callback (3)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed, supply a `token_id` string at the entrypoint that `ft_withdraw` in `contracts/defuse/src/contract/intents/state.rs` re-parses with `unwrap_or_else(|e| panic!(...))`, so the callback aborts and the transferred balance is stranded, breaking the invariant `every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [contracts/defuse/src/contract/intents/state.rs](contracts/defuse/src/contract/intents/state.rs) - `ft_withdraw` (cross-check `notify_on_transfer` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` to a receiver contract the attacker deployed
- Attacker controls: `receiver_id`, `token_ids`, `amounts`, `memo`, `msg`, and the receiver's return value
- Exploit idea: `mt_resolve_transfer` re-parses `token_ids` from strings that originated in caller-supplied arguments; a value that passes the outbound path but fails the inbound parse freezes the refund. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Fuzz `mt_batch_transfer_call` token ids; assert every accepted id parses in the resolver.
