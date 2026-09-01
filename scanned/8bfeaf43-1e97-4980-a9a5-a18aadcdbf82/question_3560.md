# Q3560: str - mul_div overflow silently truncating (7)

## Question
Given the trade is split into many unit-amount legs, can an unprivileged attacker, entering through `simulate_intents` used to quote a price a counterparty then settles against, reach `ParseDecimalError` in `crates/primitives/decimal/src/str.rs` with operands whose intermediate product exceeds the accumulator so the checked helper returns `None` and the caller substitutes a default rather than aborting, breaking the invariant `an arithmetic failure in fee or price computation always aborts, never yields a default` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/decimal/src/str.rs](crates/primitives/decimal/src/str.rs) - `ParseDecimalError`
- Entrypoint: `simulate_intents` used to quote a price a counterparty then settles against
- Attacker controls: the simulated batch and the divergence from what is later executed
- Exploit idea: Trace every `?`/`ok_or`/`unwrap_or` around `checked_mul_div_ceil` and `checked_mul_div_euclid`. Set-up: the trade is split into many unit-amount legs.
- Invariant to test: an arithmetic failure in fee or price computation always aborts, never yields a default
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Feed overflowing operands through each caller of `ParseDecimalError`; assert an error propagates.
