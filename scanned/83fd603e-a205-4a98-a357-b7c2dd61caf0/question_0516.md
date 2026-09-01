# Q0516: tlb - borsh deserialization accepts trailing bytes (3)

## Question
Given the JSON is hand-written rather than produced by a wallet, can an unprivileged attacker, entering through `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments, append or reshape bytes so `AsBoC` in `crates/serde-utils/src/tlb.rs` deserialises successfully while leaving input unconsumed, producing two distinct byte-strings that decode to the same value, breaking the invariant `each decodable value has exactly one accepted byte encoding` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [crates/serde-utils/src/tlb.rs](crates/serde-utils/src/tlb.rs) - `AsBoC` (cross-check `serialize_as` in the same file)
- Entrypoint: `mt_batch_transfer_call` / `mt_withdraw` with attacker-chosen string arguments
- Attacker controls: `token_ids`, `memo`, `msg` and every numeric field supplied as a string
- Exploit idea: Any structure used as a signed pre-image, a storage key, or a nonce must have exactly one encoding; trailing slack breaks that. Set-up: the JSON is hand-written rather than produced by a wallet.
- Invariant to test: each decodable value has exactly one accepted byte encoding
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Property-test `AsBoC` with appended bytes; assert deserialisation rejects unconsumed input.
