# Q4174: ops - Pips::invert / MAX boundary (12)

## Question
Given the token type is one `token_fee` exempts from the fee, can an unprivileged attacker, entering through an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification, drive `checked_div` in `crates/primitives/decimal/src/ops.rs` to a `Pips` value at or near `Pips::MAX` where `invert()` or the `mul_div` denominator becomes zero or wraps, so the fee computation returns a value unrelated to the configured rate, breaking the invariant `the fee applied == the configured `Pips` rate, for every representable rate` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/decimal/src/ops.rs](crates/primitives/decimal/src/ops.rs) - `checked_div` (cross-check `checked_mul_ceil` in the same file)
- Entrypoint: an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification
- Attacker controls: the token type and the per-leg amounts
- Exploit idea: The denominator is `token_fee(...).invert().as_pips()`; at the extreme this can be zero, and the `checked_*` result may be discarded by the caller. Set-up: the token type is one `token_fee` exempts from the fee.
- Invariant to test: the fee applied == the configured `Pips` rate, for every representable rate
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Table-test `checked_div` across `Pips::ZERO`, `ONE_PIP`, `MAX` and adjacent values; assert no division-by-zero or wrap.
