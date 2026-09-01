# Q2104: lib - WebAuthn client-data challenge not bound to the payload (3)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, get `WebauthnAssertion` in `crates/signatures/webauthn/src/lib.rs` to accept an assertion whose `client_data_json` challenge, origin or type does not bind the exact `payload` string the contract goes on to execute, breaking the invariant `the challenge inside the verified assertion == the digest of the `payload` string the contract executes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/webauthn/src/lib.rs](crates/signatures/webauthn/src/lib.rs) - `WebauthnAssertion` (cross-check `UserVerification` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Reuse a genuine assertion the victim produced for one payload against a different payload whose digest the challenge check does not actually constrain. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the challenge inside the verified assertion == the digest of the `payload` string the contract executes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Take a valid assertion, swap the `payload` field, and assert `WebauthnAssertion` rejects it.
