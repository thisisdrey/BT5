# Q5999: lib - unbounded sub-id length breaks a downstream consumer (11)

## Question
Given an `ImtMint` has already bound that id to the attacker as minter, can an unprivileged attacker, entering through an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter, supply a sub-token id through `TokenId` in `crates/primitives/token-id/src/lib.rs` long enough to break the storage key, the event encoding, or the resolver's re-parse, after balances have already moved, breaking the invariant `every constructible `TokenId` satisfies the length bound the contract relies on` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/primitives/token-id/src/lib.rs](crates/primitives/token-id/src/lib.rs) - `TokenId` (cross-check `from_str` in the same file)
- Entrypoint: an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter
- Attacker controls: the `token_ids` in `ImtTokens` and the `receiver_id`
- Exploit idea: The type comment notes construction goes through `TokenId` to check length; probe every constructor that bypasses it. Set-up: an `ImtMint` has already bound that id to the attacker as minter.
- Invariant to test: every constructible `TokenId` satisfies the length bound the contract relies on
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Construct maximum-length ids via each public path; assert the length check is enforced.
