# Q3795: near - digest backend divergence between host and wasm (26)

## Question
Given the attacker has already deposited 1 unit to the victim's id, so the account entry exists and the implicit fallback no longer applies, can an unprivileged attacker, entering through `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority, exploit a difference between the `digest` implementation in `crates/digest/src/sha2/near.rs` and the NEAR host function it wraps, so the same input yields different digests on-chain and off-chain, breaking the invariant `digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/digest/src/sha2/near.rs](crates/digest/src/sha2/near.rs) - `digest` (cross-check `Sha256Fn` in the same file)
- Entrypoint: `execute_intents(signed: Vec<MultiPayload>)` - callable by ANY account; the signature is the only authority
- Attacker controls: the full `MultiPayload` bytes: standard tag, envelope fields, public key, signature, and the embedded `DefusePayload` JSON
- Exploit idea: Target input lengths at the block boundary, empty input, or the `near`-feature-gated path versus the pure-Rust path used by wallets and relayers to precompute the hash. Set-up: the attacker has already deposited 1 unit to the victim's id, so the account entry exists and the implicit fallback no longer applies.
- Invariant to test: digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `digest` against a reference implementation over random and boundary-length inputs; assert byte equality.
