# Q1156: curve - deterministic nonce or key derivation collision (2)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, cause two distinct inputs to `Curve` in `crates/crypto/src/curve.rs` to derive the same key, tweak or account identity, so one party's authorisation applies to another's assets, breaking the invariant `distinct derivation inputs to `Curve` == distinct derived outputs` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/crypto/src/curve.rs](crates/crypto/src/curve.rs) - `Curve` (cross-check `recover` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Attack the concatenation used before hashing: unescaped separators, attacker-chosen sub-identifiers, or a length field that is not committed. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: distinct derivation inputs to `Curve` == distinct derived outputs
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `Curve` for collisions across adversarially chosen inputs containing the separator byte.
