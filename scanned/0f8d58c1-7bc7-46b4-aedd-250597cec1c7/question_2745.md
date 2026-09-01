# Q2745: webauthn - verifying_contract binding bypass

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, reuse a payload signed for a different deployment by making `verify` in `contracts/defuse/core/src/payload/webauthn.rs` produce a `verifying_contract` that equals `env::current_account_id()` on the target while the signer believed it was signing for another contract, breaking the invariant `the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/webauthn.rs](contracts/defuse/core/src/payload/webauthn.rs) - `verify` (cross-check `extract_defuse_payload` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Attack the `recipient.parse()` / domain-separator step: casing, trailing dot, unicode, or an envelope field the signer's wallet renders differently from what the contract compares. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Assert `extract_defuse_payload()` rejects every `recipient` string that is not byte-identical to the deployed account id.
