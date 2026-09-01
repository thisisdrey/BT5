# Q3936: lib - digest backend divergence between host and wasm (47)

## Question
Given the attacker has already deposited 1 unit to the victim's id, so the account entry exists and the implicit fallback no longer applies, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, exploit a difference between the `SignedMessageNep` implementation in `crates/signatures/nep461/src/lib.rs` and the NEAR host function it wraps, so the same input yields different digests on-chain and off-chain, breaking the invariant `digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/nep461/src/lib.rs](crates/signatures/nep461/src/lib.rs) - `SignedMessageNep` (cross-check `OffchainMessage` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Target input lengths at the block boundary, empty input, or the `near`-feature-gated path versus the pure-Rust path used by wallets and relayers to precompute the hash. Set-up: the attacker has already deposited 1 unit to the victim's id, so the account entry exists and the implicit fallback no longer applies.
- Invariant to test: digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `SignedMessageNep` against a reference implementation over random and boundary-length inputs; assert byte equality.
