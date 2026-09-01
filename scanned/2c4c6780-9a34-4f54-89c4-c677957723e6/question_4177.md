# Q4177: hex - borsh deserialization accepts trailing bytes (12)

## Question
Given the encoding carries trailing bytes beyond the declared structure, can an unprivileged attacker, entering through a value the attacker's contract returns into a resolve callback, append or reshape bytes so `AsHex` in `crates/serde-utils/src/hex.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/serde-utils/src/hex.rs](crates/serde-utils/src/hex.rs) - `AsHex`
- Entrypoint: a value the attacker's contract returns into a resolve callback
- Attacker controls: the exact JSON bytes returned
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the encoding carries trailing bytes beyond the declared structure.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `AsHex` with appended bytes; assert deserialisation rejects unconsumed input.
