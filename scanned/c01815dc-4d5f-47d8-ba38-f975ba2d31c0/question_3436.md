# Q3436: ops - mul_div overflow silently truncating (6)

## Question
Given the trade is split into many unit-amount legs, can an unprivileged attacker, entering through `execute_intents` where the attacker acts as the solver quoting against a published closure, reach `checked_div_ceil` in `crates/primitives/decimal/src/ops.rs` with operands whose intermediate product exceeds the accumulator so the checked helper returns `None` and the caller substitutes a default rather than aborting, breaking the invariant `an arithmetic failure in fee or price computation always aborts, never yields a default` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/decimal/src/ops.rs](crates/primitives/decimal/src/ops.rs) - `checked_div_ceil` (cross-check `checked_mul_ceil` in the same file)
- Entrypoint: `execute_intents` where the attacker acts as the solver quoting against a published closure
- Attacker controls: the closure they publish and the deltas they actually sign
- Exploit idea: Trace every `?`/`ok_or`/`unwrap_or` around `checked_mul_div_ceil` and `checked_mul_div_euclid`. Set-up: the trade is split into many unit-amount legs.
- Invariant to test: an arithmetic failure in fee or price computation always aborts, never yields a default
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Feed overflowing operands through each caller of `checked_div_ceil`; assert an error propagates.
