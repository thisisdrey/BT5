# Q1655: nep171 - Display/FromStr round-trip breaks for an attacker-chosen sub-id (4)

## Question
Given the sub-token id is the empty string, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract, register a token whose `from_str` string form in `crates/primitives/token-id/src/nep171.rs` does not round-trip through `FromStr`, so the id written into balances differs from the id read back by a resolver, an event consumer or a later withdrawal, breaking the invariant ``TokenId::from_str(&t.to_string()) == t` for every `t` the contract can construct` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/nep171.rs](crates/primitives/token-id/src/nep171.rs) - `from_str` (cross-check `Nep171TokenId` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` from an attacker-deployed token or multi-token contract
- Attacker controls: the contract account id and the `mt_token_id` / NFT `token_id` string it reports
- Exploit idea: `split_once(':')` takes the first colon only; sub-ids may contain colons, empty strings, or characters that re-parse into a different variant. Set-up: the sub-token id is the empty string.
- Invariant to test: `TokenId::from_str(&t.to_string()) == t` for every `t` the contract can construct
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `from_str` round-trip over sub-ids containing ':', '', and unicode; assert equality.
