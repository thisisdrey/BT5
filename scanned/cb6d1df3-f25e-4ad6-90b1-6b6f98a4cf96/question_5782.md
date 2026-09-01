# Q5782: sep53 - public-key type confusion across curves (6)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, get `extract_defuse_payload` in `contracts/defuse/core/src/payload/sep53.rs` to accept a `Signature` variant paired with a `PublicKey` variant of a different curve, or to coerce an attacker key into the victim's registered key type, breaking the invariant `the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/sep53.rs](contracts/defuse/core/src/payload/sep53.rs) - `extract_defuse_payload` (cross-check `verify` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Target the match arms pairing `(PublicKey::Ed25519, Signature::Ed25519)` / `(PublicKey::P256, Signature::P256)` and any `try_into().ok()?` that silently discards a malformed key rather than rejecting the payload. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the curve of the verified signature == the curve of the `PublicKey` registered on the signer's account
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `extract_defuse_payload` with mismatched key/signature variants and with keys whose `try_into()` fails; assert no arm returns `Some`.
