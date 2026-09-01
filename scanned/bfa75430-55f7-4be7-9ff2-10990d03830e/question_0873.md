# Q0873: nep141 - unbounded sub-id length breaks a downstream consumer (2)

## Question
Given the attacker's token contract reports a sub-token id containing ':', can an unprivileged attacker, entering through an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter, supply a sub-token id through `from_str` in `crates/primitives/token-id/src/nep141.rs` long enough to break the storage key, the event encoding, or the resolver's re-parse, after balances have already moved, breaking the invariant `every constructible `TokenId` satisfies the length bound the contract relies on` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/primitives/token-id/src/nep141.rs](crates/primitives/token-id/src/nep141.rs) - `from_str` (cross-check `Nep141TokenId` in the same file)
- Entrypoint: an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter
- Attacker controls: the `token_ids` in `ImtTokens` and the `receiver_id`
- Exploit idea: The type comment notes construction goes through `TokenId` to check length; probe every constructor that bypasses it. Set-up: the attacker's token contract reports a sub-token id containing ':'.
- Invariant to test: every constructible `TokenId` satisfies the length bound the contract relies on
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Construct maximum-length ids via each public path; assert the length check is enforced.
