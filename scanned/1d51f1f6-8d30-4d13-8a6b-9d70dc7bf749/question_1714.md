# Q1714: lib - WebAuthn client-data challenge not bound to the payload

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, get `Webauthn` in `crates/signatures/webauthn/src/lib.rs` to accept an assertion whose `client_data_json` challenge, origin or type does not bind the exact `payload` string the contract goes on to execute, breaking the invariant `the challenge inside the verified assertion == the digest of the `payload` string the contract executes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/webauthn/src/lib.rs](crates/signatures/webauthn/src/lib.rs) - `Webauthn` (cross-check `WebauthnAssertion` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Reuse a genuine assertion the victim produced for one payload against a different payload whose digest the challenge check does not actually constrain. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the challenge inside the verified assertion == the digest of the `payload` string the contract executes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Take a valid assertion, swap the `payload` field, and assert `Webauthn` rejects it.
