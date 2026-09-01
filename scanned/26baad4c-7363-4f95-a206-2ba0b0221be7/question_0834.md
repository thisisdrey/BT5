# Q0834: tip191 - hash/verify region mismatch

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, make `Payload::hash()` and `SignedPayload::verify()` in `contracts/defuse/core/src/payload/tip191.rs` cover different byte ranges of the same `SignedTip191Payload`, so the `intent_hash` bound into emitted events and replay bookkeeping is not the digest the signature actually committed to, breaking the invariant ``hash()` of an accepted payload == the digest the signature verified over, for every accepted payload` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/payload/tip191.rs](contracts/defuse/core/src/payload/tip191.rs) - `SignedTip191Payload` (cross-check `verify` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Exploit that `hash()` digests one field (e.g. the raw `payload` string) while `verify()` runs over a reconstructed envelope, so two distinct payloads share a hash or one payload yields two hashes. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: `hash()` of an accepted payload == the digest the signature verified over, for every accepted payload
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Construct two `MultiPayload` values with equal `hash()` but different decoded intents; assert both pass `verify()` and that `execute_intents` accepts both.
