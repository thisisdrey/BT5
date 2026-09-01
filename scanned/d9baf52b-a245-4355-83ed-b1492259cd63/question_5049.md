# Q5049: tip191 - cross-standard envelope confusion (6)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, produce a single byte-string that `SignedTip191Payload` in `contracts/defuse/core/src/payload/tip191.rs` accepts as a valid signature under one `MultiPayload` variant while `extract_defuse_payload()` decodes it into a `DefusePayload` naming a different `signer_id` than the key actually signed for, breaking the invariant `the `(signer_id, public_key)` pair `Engine::execute_signed_intent` authorises == the pair the private-key holder actually signed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/tip191.rs](contracts/defuse/core/src/payload/tip191.rs) - `SignedTip191Payload` (cross-check `verify` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Build the payload so the standard-specific envelope (prefix, length field, domain separator) consumed by `SignedTip191Payload` covers fewer bytes than the JSON that `extract_defuse_payload()` later parses, so the verified region and the authorised region differ. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the `(signer_id, public_key)` pair `Engine::execute_signed_intent` authorises == the pair the private-key holder actually signed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `SignedTip191Payload` with a payload whose verified envelope and parsed `DefusePayload` disagree; assert `verify()` returns `Some(pk)` while the decoded `signer_id` is not `pk.to_implicit_account_id()` and is an account the attacker does not control.
