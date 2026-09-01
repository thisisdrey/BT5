# Q5021: lib - Pips::invert / MAX boundary (13)

## Question
Given the counterparty settles against a published `simulate_intents` quote, can an unprivileged attacker, entering through `execute_intents` with a `TokenDiff` shaped to exercise the fee path, drive `checked_div` in `crates/primitives/fees/src/lib.rs` to a `Pips` value at or near `Pips::MAX` where `invert()` or the `mul_div` denominator becomes zero or wraps, so the fee computation returns a value unrelated to the configured rate, breaking the invariant `the fee applied == the configured `Pips` rate, for every representable rate` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/fees/src/lib.rs](crates/primitives/fees/src/lib.rs) - `checked_div` (cross-check `checked_mul` in the same file)
- Entrypoint: `execute_intents` with a `TokenDiff` shaped to exercise the fee path
- Attacker controls: the sign, magnitude and token type of every delta, and the number of legs
- Exploit idea: The denominator is `token_fee(...).invert().as_pips()`; at the extreme this can be zero, and the `checked_*` result may be discarded by the caller. Set-up: the counterparty settles against a published `simulate_intents` quote.
- Invariant to test: the fee applied == the configured `Pips` rate, for every representable rate
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Table-test `checked_div` across `Pips::ZERO`, `ONE_PIP`, `MAX` and adjacent values; assert no division-by-zero or wrap.
