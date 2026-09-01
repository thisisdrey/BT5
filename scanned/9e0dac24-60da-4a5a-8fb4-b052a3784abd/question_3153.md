# Q3153: nep245 - unbounded sub-id length breaks a downstream consumer (4)

## Question
Given the sub-token id is the empty string, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract, supply a sub-token id through `from_str` in `crates/primitives/token-id/src/nep245.rs` long enough to break the storage key, the event encoding, or the resolver's re-parse, after balances have already moved, breaking the invariant `every constructible `TokenId` satisfies the length bound the contract relies on` and leading to permanent freezing of user funds (unrecoverable without a privileged action)?

## Target
- File/function: [crates/primitives/token-id/src/nep245.rs](crates/primitives/token-id/src/nep245.rs) - `from_str` (cross-check `Nep245TokenId` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract
- Attacker controls: the contract account id and the `mt_token_id` / NFT `token_id` string it reports
- Exploit idea: The type comment notes construction goes through `TokenId` to check length; probe every constructor that bypasses it. Set-up: the sub-token id is the empty string.
- Invariant to test: every constructible `TokenId` satisfies the length bound the contract relies on
- Expected Immunefi impact: Critical - Permanent freezing of user funds (unrecoverable without a privileged action)
- Fast validation: Construct maximum-length ids via each public path; assert the length check is enforced.
