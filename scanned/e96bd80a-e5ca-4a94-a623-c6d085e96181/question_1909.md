# Q1909: lib - WebAuthn client-data challenge not bound to the payload (2)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, get `verify` in `crates/signatures/webauthn/src/lib.rs` to accept an assertion whose `client_data_json` challenge, origin or type does not bind the exact `payload` string the contract goes on to execute, breaking the invariant `the challenge inside the verified assertion == the digest of the `payload` string the contract executes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/webauthn/src/lib.rs](crates/signatures/webauthn/src/lib.rs) - `verify` (cross-check `maybe_prehash` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Reuse a genuine assertion the victim produced for one payload against a different payload whose digest the challenge check does not actually constrain. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the challenge inside the verified assertion == the digest of the `payload` string the contract executes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Take a valid assertion, swap the `payload` field, and assert `verify` rejects it.
