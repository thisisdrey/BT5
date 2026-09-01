# Q0736: ed25519 - public-key type confusion across curves (7)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), get `maybe_prehash` in `crates/signatures/webauthn/src/ed25519.rs` to accept a `Signature` variant paired with a `PublicKey` variant of a different curve, or to coerce an attacker key into the victim's registered key type, breaking the invariant `the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/webauthn/src/ed25519.rs](crates/signatures/webauthn/src/ed25519.rs) - `maybe_prehash` (cross-check `Ed25519` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Target the match arms pairing `(PublicKey::Ed25519, Signature::Ed25519)` / `(PublicKey::P256, Signature::P256)` and any `try_into().ok()?` that silently discards a malformed key rather than rejecting the payload. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `maybe_prehash` with mismatched key/signature variants and with keys whose `try_into()` fails; assert no arm returns `Some`.
