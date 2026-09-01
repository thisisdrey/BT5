# Q2412: lib - borsh discriminant vs string form disagreement (4)

## Question
Given the attacker's token contract reports a sub-token id containing ':', can an unprivileged attacker, entering through a `TokenDiff` naming attacker-chosen `TokenId` keys, exploit a difference between the borsh `use_discriminant` encoding of `from_str` in `crates/primitives/token-id/src/lib.rs` (used for storage keys) and the `Display` form (used in events and arguments), so two representations of the same balance diverge, breaking the invariant `borsh-equality of two `TokenId` values == string-equality of the same two values` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/lib.rs](crates/primitives/token-id/src/lib.rs) - `from_str` (cross-check `TokenId` in the same file)
- Entrypoint: a `TokenDiff` naming attacker-chosen `TokenId` keys
- Attacker controls: the token id strings used as map keys in the signed diff
- Exploit idea: Storage is keyed by the borsh form; arguments and events use the string form. A value that encodes equal in one and unequal in the other splits or merges balances. Set-up: the attacker's token contract reports a sub-token id containing ':'.
- Invariant to test: borsh-equality of two `TokenId` values == string-equality of the same two values
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test both encodings for agreement across random and adversarial ids.
