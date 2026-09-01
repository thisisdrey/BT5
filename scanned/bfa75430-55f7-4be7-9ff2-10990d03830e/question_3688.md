# Q3688: lib - digest backend divergence between host and wasm (43)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), exploit a difference between the `recover` implementation in `crates/signatures/erc191/src/lib.rs` and the NEAR host function it wraps, so the same input yields different digests on-chain and off-chain, breaking the invariant `digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/signatures/erc191/src/lib.rs](crates/signatures/erc191/src/lib.rs) - `recover` (cross-check `prehash` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Target input lengths at the block boundary, empty input, or the `near`-feature-gated path versus the pure-Rust path used by wallets and relayers to precompute the hash. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `recover` against a reference implementation over random and boundary-length inputs; assert byte equality.
