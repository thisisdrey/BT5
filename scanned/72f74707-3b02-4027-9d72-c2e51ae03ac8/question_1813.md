# Q1813: public_key - serde flatten field shadowing in the decoded payload

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, craft JSON where `#[serde(flatten)]` on the message body of `contracts/defuse/core/src/public_key.rs` lets a duplicate or shadowing key override `signer_id`, `verifying_contract`, `deadline` or `nonce` after `PublicKey` has already verified the outer envelope, breaking the invariant `every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/public_key.rs](contracts/defuse/core/src/public_key.rs) - `PublicKey` (cross-check `example_secp256k1` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Supply the same field twice, or place a message-level field with the same name as a `DefusePayload` field, and check which value `serde_json` keeps versus which the signer saw. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a JSON payload with duplicated `signer_id` keys through `extract_defuse_payload()`; assert the decoded value equals the one the signer intended.
