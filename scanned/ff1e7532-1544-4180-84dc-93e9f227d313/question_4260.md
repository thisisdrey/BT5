# Q4260: nep245 - cross-variant confusion between Imt and Nep245 (10)

## Question
Given the same sub-token id already exists under a different `TokenIdType`, can an unprivileged attacker, entering through an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter, exploit that `ImtTokenId` and `Nep245TokenId` in `crates/primitives/token-id/src/nep245.rs` share the `account:sub` shape, so a token minted under one variant is spent or withdrawn as the other through `from_str`, breaking the invariant `the variant a balance was credited under == the variant it is debited under` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/nep245.rs](crates/primitives/token-id/src/nep245.rs) - `from_str` (cross-check `Nep245TokenId` in the same file)
- Entrypoint: an `ImtMint` intent inside `execute_intents`, which binds the token id to the signer as minter
- Attacker controls: the `token_ids` in `ImtTokens` and the `receiver_id`
- Exploit idea: The discriminant prefix separates them at the `TokenId` level; probe every place a sub-id is re-parsed without its prefix, or where the prefix is reconstructed rather than carried. Set-up: the same sub-token id already exists under a different `TokenIdType`.
- Invariant to test: the variant a balance was credited under == the variant it is debited under
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Mint an `imt:` token and attempt to withdraw it via the NEP-245 path; assert rejection.
