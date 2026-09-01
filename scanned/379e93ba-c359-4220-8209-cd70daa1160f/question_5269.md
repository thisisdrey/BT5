# Q5269: lib - mul_div overflow silently truncating (25)

## Question
Given the counterparty settles against a published `simulate_intents` quote, can an unprivileged attacker, entering through `execute_intents` with a `TokenDiff` shaped to exercise the fee path, reach `from_pips` in `crates/primitives/fees/src/lib.rs` with operands whose intermediate product exceeds the accumulator so the checked helper returns `None` and the caller substitutes a default rather than aborting, breaking the invariant `an arithmetic failure in fee or price computation always aborts, never yields a default` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/fees/src/lib.rs](crates/primitives/fees/src/lib.rs) - `from_pips` (cross-check `checked_div` in the same file)
- Entrypoint: `execute_intents` with a `TokenDiff` shaped to exercise the fee path
- Attacker controls: the sign, magnitude and token type of every delta, and the number of legs
- Exploit idea: Trace every `?`/`ok_or`/`unwrap_or` around `checked_mul_div_ceil` and `checked_mul_div_euclid`. Set-up: the counterparty settles against a published `simulate_intents` quote.
- Invariant to test: an arithmetic failure in fee or price computation always aborts, never yields a default
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Feed overflowing operands through each caller of `from_pips`; assert an error propagates.
