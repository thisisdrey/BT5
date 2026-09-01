# Q0716: ops - Pips::invert / MAX boundary (4)

## Question
Given the protocol fee is set to a small non-zero rate, can an unprivileged attacker, entering through an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification, drive `checked_mul_ceil` in `crates/primitives/decimal/src/ops.rs` to a `Pips` value at or near `Pips::MAX` where `invert()` or the `mul_div` denominator becomes zero or wraps, so the fee computation returns a value unrelated to the configured rate, breaking the invariant `the fee applied == the configured `Pips` rate, for every representable rate` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/decimal/src/ops.rs](crates/primitives/decimal/src/ops.rs) - `checked_mul_ceil` (cross-check `checked_div_ceil` in the same file)
- Entrypoint: an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification
- Attacker controls: the token type and the per-leg amounts
- Exploit idea: The denominator is `token_fee(...).invert().as_pips()`; at the extreme this can be zero, and the `checked_*` result may be discarded by the caller. Set-up: the protocol fee is set to a small non-zero rate.
- Invariant to test: the fee applied == the configured `Pips` rate, for every representable rate
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Table-test `checked_mul_ceil` across `Pips::ZERO`, `ONE_PIP`, `MAX` and adjacent values; assert no division-by-zero or wrap.
