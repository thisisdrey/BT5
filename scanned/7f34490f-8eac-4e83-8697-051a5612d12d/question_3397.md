# Q3397: nep141 - unbounded sub-id length breaks a downstream consumer (9)

## Question
Given the same sub-token id already exists under a different `TokenIdType`, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` with attacker-chosen `token_ids` strings, supply a sub-token id through `Nep141TokenId` in `crates/primitives/token-id/src/nep141.rs` long enough to break the storage key, the event encoding, or the resolver's re-parse, after balances have already moved, breaking the invariant `every constructible `TokenId` satisfies the length bound the contract relies on` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/primitives/token-id/src/nep141.rs](crates/primitives/token-id/src/nep141.rs) - `Nep141TokenId` (cross-check `from_str` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` with attacker-chosen `token_ids` strings
- Attacker controls: every `token_id` string in the vector
- Exploit idea: The type comment notes construction goes through `TokenId` to check length; probe every constructor that bypasses it. Set-up: the same sub-token id already exists under a different `TokenIdType`.
- Invariant to test: every constructible `TokenId` satisfies the length bound the contract relies on
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Construct maximum-length ids via each public path; assert the length check is enforced.
