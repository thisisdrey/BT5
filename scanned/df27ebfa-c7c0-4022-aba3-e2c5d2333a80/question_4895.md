# Q4895: lib - rounding direction favours the payer (15)

## Question
Given the counterparty settles against a published `simulate_intents` quote, can an unprivileged attacker, entering through `simulate_intents` used to quote a price a counterparty then settles against, shape a trade so `PipsOutOfRange` in `crates/primitives/fees/src/lib.rs` rounds the protocol fee down (or the required counterparty amount down) on every leg, so many small legs collect materially less than one large leg, breaking the invariant `fees collected for a notional N == fees collected for the same N split across any number of legs` and leading to theft of unclaimed protocol fees / protocol fee bypass?

## Target
- File/function: [crates/primitives/fees/src/lib.rs](crates/primitives/fees/src/lib.rs) - `PipsOutOfRange` (cross-check `Pips` in the same file)
- Entrypoint: `simulate_intents` used to quote a price a counterparty then settles against
- Attacker controls: the simulated batch and the divergence from what is later executed
- Exploit idea: `fee_ceil` rounds up but `closure_supply_delta` uses `checked_mul_div_euclid`; probe where the two disagree and whether splitting is profitable. Set-up: the counterparty settles against a published `simulate_intents` quote.
- Invariant to test: fees collected for a notional N == fees collected for the same N split across any number of legs
- Expected Immunefi impact: High - Theft of unclaimed protocol fees / protocol fee bypass
- Fast validation: Sweep leg counts for a fixed notional; assert total fee is non-decreasing as legs increase.
