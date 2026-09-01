# Q3580: lib - empty / default signature accepted (15)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, pass an all-zero, empty, or default-valued signature or public key through `WebauthnAssertion` in `crates/signatures/webauthn/src/lib.rs` and reach an arm that treats it as valid, breaking the invariant ``WebauthnAssertion` never returns `Some` for a default-constructed or all-zero signature` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/webauthn/src/lib.rs](crates/signatures/webauthn/src/lib.rs) - `WebauthnAssertion` (cross-check `Webauthn` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Check whether any code path short-circuits on a default `Signature`/`PublicKey` before doing real verification. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `WebauthnAssertion` never returns `Some` for a default-constructed or all-zero signature
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `WebauthnAssertion` with zeroed inputs; assert rejection.
