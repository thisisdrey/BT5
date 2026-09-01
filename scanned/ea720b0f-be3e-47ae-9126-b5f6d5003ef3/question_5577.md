# Q5577: lib - rounding direction favours the payer (18)

## Question
Given the amounts sit exactly on a rounding boundary for the configured rate, can an unprivileged attacker, entering through `execute_intents` where the attacker acts as the solver quoting against a published closure, shape a trade so `PipsOutOfRange` in `crates/primitives/fees/src/lib.rs` rounds the protocol fee down (or the required counterparty amount down) on every leg, so many small legs collect materially less than one large leg, breaking the invariant `fees collected for a notional N == fees collected for the same N split across any number of legs` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/fees/src/lib.rs](crates/primitives/fees/src/lib.rs) - `PipsOutOfRange` (cross-check `fee_ceil` in the same file)
- Entrypoint: `execute_intents` where the attacker acts as the solver quoting against a published closure
- Attacker controls: the closure they publish and the deltas they actually sign
- Exploit idea: `fee_ceil` rounds up but `closure_supply_delta` uses `checked_mul_div_euclid`; probe where the two disagree and whether splitting is profitable. Set-up: the amounts sit exactly on a rounding boundary for the configured rate.
- Invariant to test: fees collected for a notional N == fees collected for the same N split across any number of legs
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Sweep leg counts for a fixed notional; assert total fee is non-decreasing as legs increase.
