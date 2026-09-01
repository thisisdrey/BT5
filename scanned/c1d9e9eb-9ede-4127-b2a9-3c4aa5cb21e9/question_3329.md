# Q3329: lib - public-key type confusion across curves (9)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, get `verify` in `crates/signatures/sep53/src/lib.rs` to accept a `Signature` variant paired with a `PublicKey` variant of a different curve, or to coerce an attacker key into the victim's registered key type, breaking the invariant `the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/sep53/src/lib.rs](crates/signatures/sep53/src/lib.rs) - `verify` (cross-check `prehash` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Target the match arms pairing `(PublicKey::Ed25519, Signature::Ed25519)` / `(PublicKey::P256, Signature::P256)` and any `try_into().ok()?` that silently discards a malformed key rather than rejecting the payload. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `verify` with mismatched key/signature variants and with keys whose `try_into()` fails; assert no arm returns `Some`.
