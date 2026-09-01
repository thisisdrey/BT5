# Q4991: signature - verifying_contract binding bypass (5)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, reuse a payload signed for a different deployment by making `example_ed25519` in `contracts/defuse/core/src/signature.rs` produce a `verifying_contract` that equals `env::current_account_id()` on the target while the signer believed it was signing for another contract, breaking the invariant `the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/signature.rs](contracts/defuse/core/src/signature.rs) - `example_ed25519` (cross-check `Signature` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Attack the `recipient.parse()` / domain-separator step: casing, trailing dot, unicode, or an envelope field the signer's wallet renders differently from what the contract compares. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the `verifying_contract` the engine compares == the contract identity the signer's wallet displayed and signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Assert `extract_defuse_payload()` rejects every `recipient` string that is not byte-identical to the deployed account id.
