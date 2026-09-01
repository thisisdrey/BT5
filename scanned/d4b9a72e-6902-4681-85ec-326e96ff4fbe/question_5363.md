# Q5363: signature - digest backend divergence between host and wasm (7)

## Question
Given the victim has registered exactly one public key, of a different curve than the one submitted, can an unprivileged attacker, entering through `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed, exploit a difference between the `example_secp256k1` implementation in `contracts/defuse/core/src/signature.rs` and the NEAR host function it wraps, so the same input yields different digests on-chain and off-chain, breaking the invariant `digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [contracts/defuse/core/src/signature.rs](contracts/defuse/core/src/signature.rs) - `example_secp256k1` (cross-check `Signature` in the same file)
- Entrypoint: `ft_on_transfer` / `mt_on_transfer` `msg` carrying `DepositAction::Execute { execute_intents }` from a token the attacker deployed
- Attacker controls: the deposit `msg` string and every `MultiPayload` nested inside it
- Exploit idea: Target input lengths at the block boundary, empty input, or the `near`-feature-gated path versus the pure-Rust path used by wallets and relayers to precompute the hash. Set-up: the victim has registered exactly one public key, of a different curve than the one submitted.
- Invariant to test: digest(input) computed on-chain == digest(input) computed by the signer's client for the same bytes
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test `example_secp256k1` against a reference implementation over random and boundary-length inputs; assert byte equality.
