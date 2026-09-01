# Q4981: erc191 - cross-standard envelope confusion (5)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, produce a single byte-string that `verify` in `contracts/defuse/core/src/payload/erc191.rs` accepts as a valid signature under one `MultiPayload` variant while `extract_defuse_payload()` decodes it into a `DefusePayload` naming a different `signer_id` than the key actually signed for, breaking the invariant `the `(signer_id, public_key)` pair `Engine::execute_signed_intent` authorises == the pair the private-key holder actually signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/erc191.rs](contracts/defuse/core/src/payload/erc191.rs) - `verify` (cross-check `extract_defuse_payload` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Build the payload so the standard-specific envelope (prefix, length field, domain separator) consumed by `verify` covers fewer bytes than the JSON that `extract_defuse_payload()` later parses, so the verified region and the authorised region differ. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the `(signer_id, public_key)` pair `Engine::execute_signed_intent` authorises == the pair the private-key holder actually signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `verify` with a payload whose verified envelope and parsed `DefusePayload` disagree; assert `verify()` returns `Some(pk)` while the decoded `signer_id` is not `pk.to_implicit_account_id()` and is an account the attacker does not control.
