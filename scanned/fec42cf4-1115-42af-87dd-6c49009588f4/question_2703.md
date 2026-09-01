# Q2703: tlb - borsh deserialization accepts trailing bytes (8)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through a value the attacker's contract returns into a resolve callback, append or reshape bytes so `deserialize_as` in `crates/serde-utils/src/tlb.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/serde-utils/src/tlb.rs](crates/serde-utils/src/tlb.rs) - `deserialize_as` (cross-check `AsBoC` in the same file)
- Entrypoint: a value the attacker's contract returns into a resolve callback
- Attacker controls: the exact JSON bytes returned
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the same key appears twice in the encoded map.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `deserialize_as` with appended bytes; assert deserialisation rejects unconsumed input.
