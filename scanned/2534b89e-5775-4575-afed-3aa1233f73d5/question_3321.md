# Q3321: lib - TON Connect / TLB cell encoding ambiguity (24)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, exploit the cell/BOC encoding in `OffchainMessage` of `crates/signatures/nep461/src/lib.rs` so two different payload trees hash to the same cell, or a payload the signer never saw deserialises from the signed cell, breaking the invariant `the payload decoded from a signed cell == the payload the signer's wallet displayed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep461/src/lib.rs](crates/signatures/nep461/src/lib.rs) - `OffchainMessage` (cross-check `OnchainMessage` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Target refs-vs-bits packing, unaligned bit lengths, or an unbounded child count that lets bytes migrate between cells. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the payload decoded from a signed cell == the payload the signer's wallet displayed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Build two distinct payload trees; assert `OffchainMessage` produces different cell hashes.
