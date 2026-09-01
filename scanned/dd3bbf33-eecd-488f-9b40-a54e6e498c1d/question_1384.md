# Q1384: core - token_id re-parse panic inside a private callback (2)

## Question
Given the receiver is a contract the attacker deployed that returns a crafted JSON value, can an unprivileged attacker, entering through a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`, supply a `token_id` string at the entrypoint that `mt_batch_transfer_call` in `crates/near/nep245/src/core.rs` re-parses with `unwrap_or_else(|e| panic!(...))`, so the callback aborts and the transferred balance is stranded, breaking the invariant `every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/near/nep245/src/core.rs](crates/near/nep245/src/core.rs) - `mt_batch_transfer_call` (cross-check `mt_transfer` in the same file)
- Entrypoint: a `FtWithdraw` / `MtWithdraw` / `NftWithdraw` / `NativeWithdraw` / `StorageDeposit` / `AuthCall` intent inside `execute_intents`
- Attacker controls: every field of the withdrawal intent, including `msg`, `min_gas`, `state_init` and `attached_deposit`
- Exploit idea: `mt_resolve_transfer` re-parses `token_ids` from strings that originated in caller-supplied arguments; a value that passes the outbound path but fails the inbound parse freezes the refund. Set-up: the receiver is a contract the attacker deployed that returns a crafted JSON value.
- Invariant to test: every `token_id` string a transfer entrypoint accepts round-trips through `TokenId::from_str` in the resolver
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Fuzz `mt_batch_transfer_call` token ids; assert every accepted id parses in the resolver.
