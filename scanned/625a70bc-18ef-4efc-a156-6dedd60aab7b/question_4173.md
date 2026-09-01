# Q4173: lib - decimal parse/format asymmetry (16)

## Question
Given the counterparty settles against a published `simulate_intents` quote, can an unprivileged attacker, entering through an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification, supply a `UD128` string to `MAX_DECIMALS` in `crates/primitives/decimal/src/lib.rs` that parses to a different value than it displays (or than an off-chain quote produced), so the executed price differs from the quoted one, breaking the invariant ``UD128::from_str(&x.to_string()) == x`, and the parsed price == the price the counterparty quoted` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/decimal/src/lib.rs](crates/primitives/decimal/src/lib.rs) - `MAX_DECIMALS` (cross-check `UD128` in the same file)
- Entrypoint: an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification
- Attacker controls: the token type and the per-leg amounts
- Exploit idea: Target leading zeros, exponent forms, maximum precision, and trailing-digit truncation in the string implementation. Set-up: the counterparty settles against a published `simulate_intents` quote.
- Invariant to test: `UD128::from_str(&x.to_string()) == x`, and the parsed price == the price the counterparty quoted
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test the `UD128` string round-trip and comparison ordering.
