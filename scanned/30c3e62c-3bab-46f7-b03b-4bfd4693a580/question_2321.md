# Q2321: cleanup - borsh deserialization accepts trailing bytes (5)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through `execute_intents` with a hand-crafted JSON payload rather than a wallet-generated one, append or reshape bytes so `DefaultEntry` in `crates/map-utils/src/cleanup.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/map-utils/src/cleanup.rs](crates/map-utils/src/cleanup.rs) - `DefaultEntry` (cross-check `DefaultMap` in the same file)
- Entrypoint: `execute_intents` with a hand-crafted JSON payload rather than a wallet-generated one
- Attacker controls: every byte of the JSON, including field order, duplicates, encodings and whitespace
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the same key appears twice in the encoded map.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `DefaultEntry` with appended bytes; assert deserialisation rejects unconsumed input.
