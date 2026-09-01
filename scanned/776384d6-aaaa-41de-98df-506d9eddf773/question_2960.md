# Q2960: lib - empty / default signature accepted (5)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, pass an all-zero, empty, or default-valued signature or public key through `verify` in `crates/signatures/sep53/src/lib.rs` and reach an arm that treats it as valid, breaking the invariant ``verify` never returns `Some` for a default-constructed or all-zero signature` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/sep53/src/lib.rs](crates/signatures/sep53/src/lib.rs) - `verify` (cross-check `prehash` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Check whether any code path short-circuits on a default `Signature`/`PublicKey` before doing real verification. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `verify` never returns `Some` for a default-constructed or all-zero signature
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `verify` with zeroed inputs; assert rejection.
