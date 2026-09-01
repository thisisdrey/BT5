# Q3808: hex - borsh deserialization accepts trailing bytes (9)

## Question
Given the encoding carries trailing bytes beyond the declared structure, can an unprivileged attacker, entering through `execute_intents` with a hand-crafted JSON payload rather than a wallet-generated one, append or reshape bytes so `AsHex` in `crates/serde-utils/src/hex.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/serde-utils/src/hex.rs](crates/serde-utils/src/hex.rs) - `AsHex`
- Entrypoint: `execute_intents` with a hand-crafted JSON payload rather than a wallet-generated one
- Attacker controls: every byte of the JSON, including field order, duplicates, encodings and whitespace
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the encoding carries trailing bytes beyond the declared structure.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `AsHex` with appended bytes; assert deserialisation rejects unconsumed input.
