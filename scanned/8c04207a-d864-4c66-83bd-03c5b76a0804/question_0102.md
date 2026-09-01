# Q0102: str - Pips::invert / MAX boundary

## Question
Given the protocol fee is set to a small non-zero rate, can an unprivileged attacker, entering through `execute_intents` with a `TokenDiff` shaped to exercise the fee path, drive `ParseDecimalError` in `crates/primitives/decimal/src/str.rs` to a `Pips` value at or near `Pips::MAX` where `invert()` or the `mul_div` denominator becomes zero or wraps, so the fee computation returns a value unrelated to the configured rate, breaking the invariant `the fee applied == the configured `Pips` rate, for every representable rate` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/decimal/src/str.rs](crates/primitives/decimal/src/str.rs) - `ParseDecimalError`
- Entrypoint: `execute_intents` with a `TokenDiff` shaped to exercise the fee path
- Attacker controls: the sign, magnitude and token type of every delta, and the number of legs
- Exploit idea: The denominator is `token_fee(...).invert().as_pips()`; at the extreme this can be zero, and the `checked_*` result may be discarded by the caller. Set-up: the protocol fee is set to a small non-zero rate.
- Invariant to test: the fee applied == the configured `Pips` rate, for every representable rate
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Table-test `ParseDecimalError` across `Pips::ZERO`, `ONE_PIP`, `MAX` and adjacent values; assert no division-by-zero or wrap.
