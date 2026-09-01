# Q4731: tip191 - empty / default signature accepted

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, pass an all-zero, empty, or default-valued signature or public key through `SignedTip191Payload` in `contracts/defuse/core/src/payload/tip191.rs` and reach an arm that treats it as valid, breaking the invariant ``SignedTip191Payload` never returns `Some` for a default-constructed or all-zero signature` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/payload/tip191.rs](contracts/defuse/core/src/payload/tip191.rs) - `SignedTip191Payload` (cross-check `extract_defuse_payload` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Check whether any code path short-circuits on a default `Signature`/`PublicKey` before doing real verification. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `SignedTip191Payload` never returns `Some` for a default-constructed or all-zero signature
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Unit-test `SignedTip191Payload` with zeroed inputs; assert rejection.
