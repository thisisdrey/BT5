# Q4257: lib - borsh discriminant vs string form disagreement (7)

## Question
Given the sub-token id is the empty string, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` with attacker-chosen `token_ids` strings, exploit a difference between the borsh `use_discriminant` encoding of `from_str` in `crates/primitives/token-id/src/lib.rs` (used for storage keys) and the `Display` form (used in events and arguments), so two representations of the same balance diverge, breaking the invariant `borsh-equality of two `TokenId` values == string-equality of the same two values` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/lib.rs](crates/primitives/token-id/src/lib.rs) - `from_str` (cross-check `TokenId` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` with attacker-chosen `token_ids` strings
- Attacker controls: every `token_id` string in the vector
- Exploit idea: Storage is keyed by the borsh form; arguments and events use the string form. A value that encodes equal in one and unequal in the other splits or merges balances. Set-up: the sub-token id is the empty string.
- Invariant to test: borsh-equality of two `TokenId` values == string-equality of the same two values
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test both encodings for agreement across random and adversarial ids.
