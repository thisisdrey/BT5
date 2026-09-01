# Q2691: near - borsh deserialization accepts trailing bytes (8)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through a value the attacker's contract returns into a resolve callback, append or reshape bytes so `contains_key` in `crates/map-utils/src/near.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/map-utils/src/near.rs](crates/map-utils/src/near.rs) - `contains_key` (cross-check `remove` in the same file)
- Entrypoint: a value the attacker's contract returns into a resolve callback
- Attacker controls: the exact JSON bytes returned
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the same key appears twice in the encoded map.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `contains_key` with appended bytes; assert deserialisation rejects unconsumed input.
