# Q2821: ops - decimal parse/format asymmetry (5)

## Question
Given the trade is split into many unit-amount legs, can an unprivileged attacker, entering through `execute_intents` with a `TokenDiff` shaped to exercise the fee path, supply a `UD128` string to `checked_div` in `crates/primitives/decimal/src/ops.rs` that parses to a different value than it displays (or than an off-chain quote produced), so the executed price differs from the quoted one, breaking the invariant ``UD128::from_str(&x.to_string()) == x`, and the parsed price == the price the counterparty quoted` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/decimal/src/ops.rs](crates/primitives/decimal/src/ops.rs) - `checked_div` (cross-check `checked_mul` in the same file)
- Entrypoint: `execute_intents` with a `TokenDiff` shaped to exercise the fee path
- Attacker controls: the sign, magnitude and token type of every delta, and the number of legs
- Exploit idea: Target leading zeros, exponent forms, maximum precision, and trailing-digit truncation in the string implementation. Set-up: the trade is split into many unit-amount legs.
- Invariant to test: `UD128::from_str(&x.to_string()) == x`, and the parsed price == the price the counterparty quoted
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test the `UD128` string round-trip and comparison ordering.
