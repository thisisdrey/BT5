# Q1617: webauthn - public-key type confusion across curves

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, get `SignedWebAuthnPayload` in `contracts/defuse/core/src/payload/webauthn.rs` to accept a `Signature` variant paired with a `PublicKey` variant of a different curve, or to coerce an attacker key into the victim's registered key type, breaking the invariant `the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/webauthn.rs](contracts/defuse/core/src/payload/webauthn.rs) - `SignedWebAuthnPayload` (cross-check `verify` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Target the match arms pairing `(PublicKey::Ed25519, Signature::Ed25519)` / `(PublicKey::P256, Signature::P256)` and any `try_into().ok()?` that silently discards a malformed key rather than rejecting the payload. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `SignedWebAuthnPayload` with mismatched key/signature variants and with keys whose `try_into()` fails; assert no arm returns `Some`.
