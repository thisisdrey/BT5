# Q1945: mod - TON Connect / TLB cell encoding ambiguity (6)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, exploit the cell/BOC encoding in `Keccak512` of `crates/digest/src/sha3/mod.rs` so two different payload trees hash to the same cell, or a payload the signer never saw deserialises from the signed cell, breaking the invariant `the payload decoded from a signed cell == the payload the signer's wallet displayed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/digest/src/sha3/mod.rs](crates/digest/src/sha3/mod.rs) - `Keccak512` (cross-check `Keccak256` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Target refs-vs-bits packing, unaligned bit lengths, or an unbounded child count that lets bytes migrate between cells. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: the payload decoded from a signed cell == the payload the signer's wallet displayed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Build two distinct payload trees; assert `Keccak512` produces different cell hashes.
