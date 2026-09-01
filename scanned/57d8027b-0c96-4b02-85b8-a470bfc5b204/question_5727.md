# Q5727: signature - public key string parsing accepting non-canonical forms (5)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, register or match a `PublicKey` in `contracts/defuse/core/src/signature.rs` via `example_ed25519` using a non-canonical string form (extra prefix, alternate base58 encoding, leading zeros) that compares unequal in storage but equal at verification time, or vice versa, breaking the invariant ``PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/signature.rs](contracts/defuse/core/src/signature.rs) - `example_ed25519` (cross-check `Signature` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Exploit the difference between the `FromStr`/`Display` round-trip used for storage keys and the byte comparison used by `has_public_key`. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: `PublicKey::from_str(&pk.to_string()) == pk` and key equality in storage == key equality at verification
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test the `PublicKey` round-trip and assert `has_public_key` agrees with the verification-time comparison for every accepted encoding.
