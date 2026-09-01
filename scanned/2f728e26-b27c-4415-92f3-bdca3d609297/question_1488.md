# Q1488: near - deterministic nonce or key derivation collision (11)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), cause two distinct inputs to `Sha256Fn` in `crates/digest/src/sha2/near.rs` to derive the same key, tweak or account identity, so one party's authorisation applies to another's assets, breaking the invariant `distinct derivation inputs to `Sha256Fn` == distinct derived outputs` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/digest/src/sha2/near.rs](crates/digest/src/sha2/near.rs) - `Sha256Fn` (cross-check `digest` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Attack the concatenation used before hashing: unescaped separators, attacker-chosen sub-identifiers, or a length field that is not committed. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: distinct derivation inputs to `Sha256Fn` == distinct derived outputs
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `Sha256Fn` for collisions across adversarially chosen inputs containing the separator byte.
