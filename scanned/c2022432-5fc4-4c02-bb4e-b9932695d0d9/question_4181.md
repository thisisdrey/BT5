# Q4181: lib - empty / default signature accepted (20)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), pass an all-zero, empty, or default-valued signature or public key through `Nep413` in `crates/signatures/nep413/src/lib.rs` and reach an arm that treats it as valid, breaking the invariant ``Nep413` never returns `Some` for a default-constructed or all-zero signature` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep413/src/lib.rs](crates/signatures/nep413/src/lib.rs) - `Nep413` (cross-check `recipient` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Check whether any code path short-circuits on a default `Signature`/`PublicKey` before doing real verification. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `Nep413` never returns `Some` for a default-constructed or all-zero signature
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `Nep413` with zeroed inputs; assert rejection.
