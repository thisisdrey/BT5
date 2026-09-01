# Q1999: erc191 - serde flatten field shadowing in the decoded payload (3)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, craft JSON where `#[serde(flatten)]` on the message body of `contracts/defuse/core/src/payload/erc191.rs` lets a duplicate or shadowing key override `signer_id`, `verifying_contract`, `deadline` or `nonce` after `SignedErc191Payload` has already verified the outer envelope, breaking the invariant `every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/erc191.rs](contracts/defuse/core/src/payload/erc191.rs) - `SignedErc191Payload` (cross-check `verify` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Supply the same field twice, or place a message-level field with the same name as a `DefusePayload` field, and check which value `serde_json` keeps versus which the signer saw. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: every field the engine reads from `DefusePayload` == the field value present in the byte-string the signature covered
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Round-trip a JSON payload with duplicated `signer_id` keys through `extract_defuse_payload()`; assert the decoded value equals the one the signer intended.
