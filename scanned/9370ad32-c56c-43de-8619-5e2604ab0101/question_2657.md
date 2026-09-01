# Q2657: imt - cross-variant confusion between Imt and Nep245 (5)

## Question
Given the sub-token id is the empty string, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract, exploit that `ImtTokenId` and `Nep245TokenId` in `crates/primitives/token-id/src/imt.rs` share the `account:sub` shape, so a token minted under one variant is spent or withdrawn as the other through `from_str`, breaking the invariant `the variant a balance was credited under == the variant it is debited under` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/imt.rs](crates/primitives/token-id/src/imt.rs) - `from_str` (cross-check `ImtTokenId` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract
- Attacker controls: the contract account id and the `mt_token_id` / NFT `token_id` string it reports
- Exploit idea: The discriminant prefix separates them at the `TokenId` level; probe every place a sub-id is re-parsed without its prefix, or where the prefix is reconstructed rather than carried. Set-up: the sub-token id is the empty string.
- Invariant to test: the variant a balance was credited under == the variant it is debited under
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Mint an `imt:` token and attempt to withdraw it via the NEP-245 path; assert rejection.
