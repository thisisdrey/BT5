# Q1356: signer - deterministic nonce or key derivation collision (3)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, cause two distinct inputs to `public_key` in `crates/crypto/src/signer.rs` to derive the same key, tweak or account identity, so one party's authorisation applies to another's assets, breaking the invariant `distinct derivation inputs to `public_key` == distinct derived outputs` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/crypto/src/signer.rs](crates/crypto/src/signer.rs) - `public_key` (cross-check `Signer` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Attack the concatenation used before hashing: unescaped separators, attacker-chosen sub-identifiers, or a length field that is not committed. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: distinct derivation inputs to `public_key` == distinct derived outputs
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `public_key` for collisions across adversarially chosen inputs containing the separator byte.
