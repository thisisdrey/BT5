# Q2083: ops - mul_div overflow silently truncating (3)

## Question
Given the protocol fee is set to a small non-zero rate, can an unprivileged attacker, entering through `simulate_intents` used to quote a price a counterparty then settles against, reach `checked_div_ceil` in `crates/primitives/decimal/src/ops.rs` with operands whose intermediate product exceeds the accumulator so the checked helper returns `None` and the caller substitutes a default rather than aborting, breaking the invariant `an arithmetic failure in fee or price computation always aborts, never yields a default` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/decimal/src/ops.rs](crates/primitives/decimal/src/ops.rs) - `checked_div_ceil` (cross-check `checked_mul` in the same file)
- Entrypoint: `simulate_intents` used to quote a price a counterparty then settles against
- Attacker controls: the simulated batch and the divergence from what is later executed
- Exploit idea: Trace every `?`/`ok_or`/`unwrap_or` around `checked_mul_div_ceil` and `checked_mul_div_euclid`. Set-up: the protocol fee is set to a small non-zero rate.
- Invariant to test: an arithmetic failure in fee or price computation always aborts, never yields a default
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Feed overflowing operands through each caller of `checked_div_ceil`; assert an error propagates.
