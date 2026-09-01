# Q3581: p256 - TON Connect / TLB cell encoding ambiguity (7)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, exploit the cell/BOC encoding in `maybe_prehash` of `crates/signatures/webauthn/src/p256.rs` so two different payload trees hash to the same cell, or a payload the signer never saw deserialises from the signed cell, breaking the invariant `the payload decoded from a signed cell == the payload the signer's wallet displayed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/webauthn/src/p256.rs](crates/signatures/webauthn/src/p256.rs) - `maybe_prehash` (cross-check `P256` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Target refs-vs-bits packing, unaligned bit lengths, or an unbounded child count that lets bytes migrate between cells. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the payload decoded from a signed cell == the payload the signer's wallet displayed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Build two distinct payload trees; assert `maybe_prehash` produces different cell hashes.
