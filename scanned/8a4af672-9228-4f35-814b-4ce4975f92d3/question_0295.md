# Q0295: near - digest backend divergence between host and wasm (4)

## Question
Given the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation, can an unprivileged attacker, entering through `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on, exploit a difference between the `Ripemd160Fn` implementation in `crates/digest/src/ripemd/near.rs` and the NEAR host function it wraps, so the same input yields different digests on-chain and off-chain, breaking the invariant `digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/digest/src/ripemd/near.rs](crates/digest/src/ripemd/near.rs) - `Ripemd160Fn` (cross-check `digest` in the same file)
- Entrypoint: `simulate_intents(signed: Vec<MultiPayload>)` - a view call any party (including a solver's quoting path) may rely on
- Attacker controls: the entire simulated batch and every field of each payload
- Exploit idea: Target input lengths at the block boundary, empty input, or the `near`-feature-gated path versus the pure-Rust path used by wallets and relayers to precompute the hash. Set-up: the victim account has no entry in `self.accounts`, so `has_public_key` falls back to implicit-key derivation.
- Invariant to test: digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `Ripemd160Fn` against a reference implementation over random and boundary-length inputs; assert byte equality.
