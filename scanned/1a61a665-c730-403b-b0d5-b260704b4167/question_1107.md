# Q1107: lib - mul_div overflow silently truncating (2)

## Question
Given the protocol fee is set to a small non-zero rate, can an unprivileged attacker, entering through `execute_intents` where the attacker acts as the solver quoting against a published closure, reach `MAX_DECIMALS` in `crates/primitives/decimal/src/lib.rs` with operands whose intermediate product exceeds the accumulator so the checked helper returns `None` and the caller substitutes a default rather than aborting, breaking the invariant `an arithmetic failure in fee or price computation always aborts, never yields a default` and leading to direct theft of user funds: custodied balances moved without the owner's authorisation?

## Target
- File/function: [crates/primitives/decimal/src/lib.rs](crates/primitives/decimal/src/lib.rs) - `MAX_DECIMALS` (cross-check `UD128` in the same file)
- Entrypoint: `execute_intents` where the attacker acts as the solver quoting against a published closure
- Attacker controls: the closure they publish and the deltas they actually sign
- Exploit idea: Trace every `?`/`ok_or`/`unwrap_or` around `checked_mul_div_ceil` and `checked_mul_div_euclid`. Set-up: the protocol fee is set to a small non-zero rate.
- Invariant to test: an arithmetic failure in fee or price computation always aborts, never yields a default
- Expected Immunefi impact: Critical - Direct theft of user funds: custodied balances moved without the owner's authorisation
- Fast validation: Feed overflowing operands through each caller of `MAX_DECIMALS`; assert an error propagates.
