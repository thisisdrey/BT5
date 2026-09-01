# Q2580: tlb - borsh deserialization accepts trailing bytes (7)

## Question
Given the same key appears twice in the encoded map, can an unprivileged attacker, entering through `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments, append or reshape bytes so `serialize_as` in `crates/serde-utils/src/tlb.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/serde-utils/src/tlb.rs](crates/serde-utils/src/tlb.rs) - `serialize_as` (cross-check `deserialize_as` in the same file)
- Entrypoint: `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments
- Attacker controls: `token_ids`, `memo`, `msg` and every numeric field supplied as a string
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the same key appears twice in the encoded map.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `serialize_as` with appended bytes; assert deserialisation rejects unconsumed input.
