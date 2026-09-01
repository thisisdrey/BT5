# Q3690: lib - TON Connect / TLB cell encoding ambiguity (30)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), exploit the cell/BOC encoding in `OffchainMessage` of `crates/signatures/nep461/src/lib.rs` so two different payload trees hash to the same cell, or a payload the signer never saw deserialises from the signed cell, breaking the invariant `the payload decoded from a signed cell == the payload the signer's wallet displayed` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep461/src/lib.rs](crates/signatures/nep461/src/lib.rs) - `OffchainMessage` (cross-check `SignedMessageNep` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Target refs-vs-bits packing, unaligned bit lengths, or an unbounded child count that lets bytes migrate between cells. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: the payload decoded from a signed cell == the payload the signer's wallet displayed
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Build two distinct payload trees; assert `OffchainMessage` produces different cell hashes.
