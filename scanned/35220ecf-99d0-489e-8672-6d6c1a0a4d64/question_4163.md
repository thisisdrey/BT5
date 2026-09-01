# Q4163: near - digest backend divergence between host and wasm (34)

## Question
Given the attacker has already deposited 1 unit to the victim's id, so the account entry exists and the implicit fallback no longer applies, can an unprivileged attacker, entering through `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`), exploit a difference between the `Ripemd160Fn` implementation in `crates/digest/src/ripemd/near.rs` and the NEAR host function it wraps, so the same input yields different digests on-chain and off-chain, breaking the invariant `digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/digest/src/ripemd/near.rs](crates/digest/src/ripemd/near.rs) - `Ripemd160Fn` (cross-check `digest` in the same file)
- Entrypoint: `execute_intents` relayed through a public relayer key (function-call access key restricted to `execute_intents`)
- Attacker controls: the payload contents; the relayer only forwards them
- Exploit idea: Target input lengths at the block boundary, empty input, or the `near`-feature-gated path versus the pure-Rust path used by wallets and relayers to precompute the hash. Set-up: the attacker has already deposited 1 unit to the victim's id, so the account entry exists and the implicit fallback no longer applies.
- Invariant to test: digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `Ripemd160Fn` against a reference implementation over random and boundary-length inputs; assert byte equality.
