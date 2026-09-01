# Q3029: nep171 - Display/FromStr round-trip breaks for an attacker-chosen sub-id (9)

## Question
Given the same sub-token id already exists under a different `TokenIdType`, can an unprivileged attacker, entering through `mt_transfer_call` / `mt_batch_transfer_call` with attacker-chosen `token_ids` strings, register a token whose `Nep171TokenId` string form in `crates/primitives/token-id/src/nep171.rs` does not round-trip through `FromStr`, so the id written into balances differs from the id read back by a resolver, an event consumer or a later withdrawal, breaking the invariant ``TokenId::from_str(&t.to_string()) == t` for every `t` the contract can construct` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/token-id/src/nep171.rs](crates/primitives/token-id/src/nep171.rs) - `Nep171TokenId` (cross-check `from_str` in the same file)
- Entrypoint: `mt_transfer_call` / `mt_batch_transfer_call` with attacker-chosen `token_ids` strings
- Attacker controls: every `token_id` string in the vector
- Exploit idea: `split_once(':')` takes the first colon only; sub-ids may contain colons, empty strings, or characters that re-parse into a different variant. Set-up: the same sub-token id already exists under a different `TokenIdType`.
- Invariant to test: `TokenId::from_str(&t.to_string()) == t` for every `t` the contract can construct
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `Nep171TokenId` round-trip over sub-ids containing ':', '', and unicode; assert equality.
