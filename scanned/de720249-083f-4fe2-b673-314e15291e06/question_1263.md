# Q1263: lib - cross-variant confusion between Imt and Nep245 (4)

## Question
Given the attacker's token contract reports a sub-token id containing ':', can an unprivileged attacker, entering through a `TokenDiff` naming attacker-chosen `TokenId` keys, exploit that `ImtTokenId` and `Nep245TokenId` in `crates/primitives/token-id/src/lib.rs` share the `account:sub` shape, so a token minted under one variant is spent or withdrawn as the other through `from_str`, breaking the invariant `the variant a balance was credited under == the variant it is debited under` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/lib.rs](crates/primitives/token-id/src/lib.rs) - `from_str` (cross-check `TokenId` in the same file)
- Entrypoint: a `TokenDiff` naming attacker-chosen `TokenId` keys
- Attacker controls: the token id strings used as map keys in the signed diff
- Exploit idea: The discriminant prefix separates them at the `TokenId` level; probe every place a sub-id is re-parsed without its prefix, or where the prefix is reconstructed rather than carried. Set-up: the attacker's token contract reports a sub-token id containing ':'.
- Invariant to test: the variant a balance was credited under == the variant it is debited under
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Mint an `imt:` token and attempt to withdraw it via the NEP-245 path; assert rejection.
