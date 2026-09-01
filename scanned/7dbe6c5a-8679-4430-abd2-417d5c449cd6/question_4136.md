# Q4136: nep171 - unbounded sub-id length breaks a downstream consumer (10)

## Question
Given an `ImtMint` has already bound that id to the attacker as minter, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract, supply a sub-token id through `from_str` in `crates/primitives/token-id/src/nep171.rs` long enough to break the storage key, the event encoding, or the resolver's re-parse, after balances have already moved, breaking the invariant `every constructible `TokenId` satisfies the length bound the contract relies on` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/primitives/token-id/src/nep171.rs](crates/primitives/token-id/src/nep171.rs) - `from_str` (cross-check `Nep171TokenId` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract
- Attacker controls: the contract account id and the `mt_token_id` / NFT `token_id` string it reports
- Exploit idea: The type comment notes construction goes through `TokenId` to check length; probe every constructor that bypasses it. Set-up: an `ImtMint` has already bound that id to the attacker as minter.
- Invariant to test: every constructible `TokenId` satisfies the length bound the contract relies on
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Construct maximum-length ids via each public path; assert the length check is enforced.
