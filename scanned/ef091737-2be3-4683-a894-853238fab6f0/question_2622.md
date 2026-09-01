# Q2622: webauthn - serde flatten field shadowing in the decoded payload (4)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), craft JSON where `#[serde(flatten)]` on the message body of `contracts/defuse/core/src/payload/webauthn.rs` lets a duplicate or shadowing key override `signer_id`, `verifying_contract`, `deadline` or `nonce` after `verify` has already verified the outer envelope, breaking the invariant `every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/webauthn.rs](contracts/defuse/core/src/payload/webauthn.rs) - `verify` (cross-check `extract_defuse_payload` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Supply the same field twice, or place a message-level field with the same name as a `DefusePayload` field, and check which value `serde_json` keeps versus which the signer saw. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a JSON payload with duplicated `signer_id` keys through `extract_defuse_payload()`; assert the decoded value equals the one the signer intended.
