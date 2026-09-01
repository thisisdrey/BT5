# Q3927: lib - decimal parse/format asymmetry (14)

## Question
Given the counterparty settles against a published `simulate_intents` quote, can an unprivileged attacker, entering through `execute_intents` where the attacker acts as the solver quoting against a published closure, supply a `UD128` string to `UD128` in `crates/primitives/decimal/src/lib.rs` that parses to a different value than it displays (or than an off-chain quote produced), so the executed price differs from the quoted one, breaking the invariant ``UD128::from_str(&x.to_string()) == x`, and the parsed price == the price the counterparty quoted` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/decimal/src/lib.rs](crates/primitives/decimal/src/lib.rs) - `UD128` (cross-check `MAX_DECIMALS` in the same file)
- Entrypoint: `execute_intents` where the attacker acts as the solver quoting against a published closure
- Attacker controls: the closure they publish and the deltas they actually sign
- Exploit idea: Target leading zeros, exponent forms, maximum precision, and trailing-digit truncation in the string implementation. Set-up: the counterparty settles against a published `simulate_intents` quote.
- Invariant to test: `UD128::from_str(&x.to_string()) == x`, and the parsed price == the price the counterparty quoted
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Property-test the `UD128` string round-trip and comparison ordering.
