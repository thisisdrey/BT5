# Q1744: p256 - `try_into().ok()?` swallowing a malformed key or signature

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, reach a branch of `P256` in `crates/crypto/src/p256.rs` where a malformed key, point-at-infinity, or non-canonical scalar is discarded with `.ok()?` in a way that changes which arm decides the result rather than rejecting outright, breaking the invariant ``P256` returns `Some(pk)` only when a cryptographically valid signature by `pk` over the exact payload exists` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/crypto/src/p256.rs](crates/crypto/src/p256.rs) - `P256` (cross-check `P256UncompressedPublicKey` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Supply values that fail conversion in one arm so control falls through to a weaker arm or to a default that still returns a public key. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `P256` returns `Some(pk)` only when a cryptographically valid signature by `pk` over the exact payload exists
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Fuzz `P256` with malformed keys/signatures; assert it never returns `Some` for an invalid pair.
