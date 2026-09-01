# Q0311: tlb - borsh deserialization accepts trailing bytes (2)

## Question
Given the JSON is hand-written rather than produced by a wallet, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string, append or reshape bytes so `deserialize_as` in `crates/serde-utils/src/tlb.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/serde-utils/src/tlb.rs](crates/serde-utils/src/tlb.rs) - `deserialize_as` (cross-check `AsBoC` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string
- Attacker controls: the whole `msg`, including whether it starts with '{' and how it is escaped
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the JSON is hand-written rather than produced by a wallet.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `deserialize_as` with appended bytes; assert deserialisation rejects unconsumed input.
