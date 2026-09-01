# Q1157: ed25519 - verifying_contract binding bypass (2)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, reuse a payload signed for a different deployment by making `Ed25519` in `crates/crypto/src/ed25519.rs` produce a `verifying_contract` that equals `env::current_account_id()` on the target while the signer believed it was signing for another contract, breaking the invariant `the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/crypto/src/ed25519.rs](crates/crypto/src/ed25519.rs) - `Ed25519` (cross-check `Ed25519PublicKey` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Attack the `recipient.parse()` / domain-separator step: casing, trailing dot, unicode, or an envelope field the signer's wallet renders differently from what the contract compares. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Assert `extract_defuse_payload()` rejects every `recipient` string that is not byte-identical to the deployed account id.
