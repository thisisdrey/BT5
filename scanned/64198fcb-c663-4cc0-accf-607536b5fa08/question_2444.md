# Q2444: cleanup - borsh deserialization accepts trailing bytes (6)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string, append or reshape bytes so `DefaultOccupiedEntry` in `crates/map-utils/src/cleanup.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/map-utils/src/cleanup.rs](crates/map-utils/src/cleanup.rs) - `DefaultOccupiedEntry` (cross-check `DefaultEntry` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` with a hand-crafted `msg` string
- Attacker controls: the whole `msg`, including whether it starts with '{' and how it is escaped
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the same key appears twice in the encoded map.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `DefaultOccupiedEntry` with appended bytes; assert deserialisation rejects unconsumed input.
