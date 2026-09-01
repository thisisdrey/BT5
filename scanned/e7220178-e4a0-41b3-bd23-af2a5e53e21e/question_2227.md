# Q2227: lib - WebAuthn client-data challenge not bound to the payload (4)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), get `maybe_prehash` in `crates/signatures/webauthn/src/lib.rs` to accept an assertion whose `client_data_json` challenge, origin or type does not bind the exact `payload` string the contract goes on to execute, breaking the invariant `the challenge inside the verified assertion == the digest of the `payload` string the contract executes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/webauthn/src/lib.rs](crates/signatures/webauthn/src/lib.rs) - `maybe_prehash` (cross-check `check` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Reuse a genuine assertion the victim produced for one payload against a different payload whose digest the challenge check does not actually constrain. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the challenge inside the verified assertion == the digest of the `payload` string the contract executes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Take a valid assertion, swap the `payload` field, and assert `maybe_prehash` rejects it.
