# Q2656: lib - rounding direction favours the payer (8)

## Question
Given the trade is split into many unit-amount legs, can an unprivileged attacker, entering through an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification, shape a trade so `from_pips` in `crates/primitives/fees/src/lib.rs` rounds the protocol fee down (or the required counterparty amount down) on every leg, so many small legs collect materially less than one large leg, breaking the invariant `fees collected for a notional N == fees collected for the same N split across any number of legs` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/fees/src/lib.rs](crates/primitives/fees/src/lib.rs) - `from_pips` (cross-check `as_pips` in the same file)
- Entrypoint: an `ImtMint`/`ImtBurn` or NFT leg chosen specifically for its fee-exempt classification
- Attacker controls: the token type and the per-leg amounts
- Exploit idea: `fee_ceil` rounds up but `closure_supply_delta` uses `checked_mul_div_euclid`; probe where the two disagree and whether splitting is profitable. Set-up: the trade is split into many unit-amount legs.
- Invariant to test: fees collected for a notional N == fees collected for the same N split across any number of legs
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Sweep leg counts for a fixed notional; assert total fee is non-decreasing as legs increase.
