# Q5297: tip191 - hash/verify region mismatch (6)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, make `Payload::hash()` and `SignedPayload::verify()` in `contracts/defuse/core/src/payload/tip191.rs` cover different byte ranges of the same `verify`, so the `intent_hash` bound into emitted events and replay bookkeeping is not the digest the signature actually committed to, breaking the invariant ``hash()` of an accepted payload == the digest the signature verified over, for every accepted payload` and leading to direct theft of user funds via replay: one signed payload settles more than once?

## Target
- File/function: [contracts/defuse/core/src/payload/tip191.rs](contracts/defuse/core/src/payload/tip191.rs) - `verify` (cross-check `SignedTip191Payload` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Exploit that `hash()` digests one field (e.g. the raw `payload` string) while `verify()` runs over a reconstructed envelope, so two distinct payloads share a hash or one payload yields two hashes. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: `hash()` of an accepted payload == the digest the signature verified over, for every accepted payload
- Expected Immunefi impact: Critical - Direct theft of user funds via replay: one signed payload settles more than once
- Fast validation: Construct two `MultiPayload` values with equal `hash()` but different decoded intents; assert both pass `verify()` and that `execute_intents` accepts both.
