# Q3729: webauthn - truncation / length-prefix ambiguity in the signed envelope

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, exploit an unlength-prefixed or ambiguously delimited field in `SignedWebAuthnPayload` of `contracts/defuse/core/src/payload/webauthn.rs` so two semantically different messages serialise to the same signed pre-image, breaking the invariant `the pre-image `SignedWebAuthnPayload` signs == a unique, unambiguous encoding of exactly one `DefusePayload`` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/webauthn.rs](contracts/defuse/core/src/payload/webauthn.rs) - `SignedWebAuthnPayload` (cross-check `verify` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Move bytes across a delimiter between adjacent variable-length fields (account id, memo, msg, token id) so the concatenation is identical but the parse differs. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the pre-image `SignedWebAuthnPayload` signs == a unique, unambiguous encoding of exactly one `DefusePayload`
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Find two distinct `DefusePayload` values whose `SignedWebAuthnPayload` pre-images are byte-equal; assert one signature authorises both.
