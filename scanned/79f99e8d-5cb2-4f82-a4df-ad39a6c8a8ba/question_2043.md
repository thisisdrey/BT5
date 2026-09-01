# Q2043: lib - borsh discriminant vs string form disagreement

## Question
Given the attacker's token contract reports a sub-token id containing ':', can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract, exploit a difference between the borsh `use_discriminant` encoding of `TokenId` in `crates/primitives/token-id/src/lib.rs` (used for storage keys) and the `Display` form (used in events and arguments), so two representations of the same balance diverge, breaking the invariant `borsh-equality of two `TokenId` values == string-equality of the same two values` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/lib.rs](crates/primitives/token-id/src/lib.rs) - `TokenId` (cross-check `from_str` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract
- Attacker controls: the contract account id and the `mt_token_id` / NFT `token_id` string it reports
- Exploit idea: Storage is keyed by the borsh form; arguments and events use the string form. A value that encodes equal in one and unequal in the other splits or merges balances. Set-up: the attacker's token contract reports a sub-token id containing ':'.
- Invariant to test: borsh-equality of two `TokenId` values == string-equality of the same two values
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test both encodings for agreement across random and adversarial ids.
