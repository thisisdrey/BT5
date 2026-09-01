# Q5758: imt - cross-variant confusion between Imt and Nep245 (18)

## Question
Given the id sits at the maximum accepted length, can an unprivileged attacker, entering through an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter, exploit that `ImtTokenId` and `Nep245TokenId` in `crates/primitives/token-id/src/imt.rs` share the `account:sub` shape, so a token minted under one variant is spent or withdrawn as the other through `from_str`, breaking the invariant `the variant a balance was credited under == the variant it is debited under` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/imt.rs](crates/primitives/token-id/src/imt.rs) - `from_str` (cross-check `ImtTokenId` in the same file)
- Entrypoint: an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter
- Attacker controls: the `token_ids` in `ImtTokens` and the `receiver_id`
- Exploit idea: The discriminant prefix separates them at the `TokenId` level; probe every place a sub-id is re-parsed without its prefix, or where the prefix is reconstructed rather than carried. Set-up: the id sits at the maximum accepted length.
- Invariant to test: the variant a balance was credited under == the variant it is debited under
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Mint an `imt:` token and attempt to withdraw it via the NEP-245 path; assert rejection.
