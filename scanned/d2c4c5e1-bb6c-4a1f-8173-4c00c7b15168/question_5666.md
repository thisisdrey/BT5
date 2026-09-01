# Q5666: public_key - serde flatten field shadowing in the decoded payload (6)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, craft JSON where `#[serde(flatten)]` on the message body of `contracts/defuse/core/src/public_key.rs` lets a duplicate or shadowing key override `signer_id`, `verifying_contract`, `deadline` or `nonce` after `example_p256` has already verified the outer envelope, breaking the invariant `every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/public_key.rs](contracts/defuse/core/src/public_key.rs) - `example_p256` (cross-check `PublicKey` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Supply the same field twice, or place a message-level field with the same name as a `DefusePayload` field, and check which value `serde_json` keeps versus which the signer saw. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a JSON payload with duplicated `signer_id` keys through `extract_defuse_payload()`; assert the decoded value equals the one the signer intended.
