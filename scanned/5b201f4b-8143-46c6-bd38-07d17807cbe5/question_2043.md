# Q2043: load_kamino_reserve: stake or integration NAV path drifts from settled accounting [a-stale-vs-fresh-reserve] [freshness]

## Question
Can an unprivileged attacker use `lending_pool_pulse_bank_price_cache` with a stale-vs-fresh reserve boundary condition so `load_kamino_reserve` values stake/integration exposure from a path that drifts from later settled accounting, breaking `external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes` and causing `High: exploitable cached misvaluation on a live integration bank`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `load_kamino_reserve`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a stale-vs-fresh reserve boundary condition
- Exploit idea: Inspect special valuation helpers where derivative asset value is inferred indirectly and must still stay consistent with how deposits/withdrawals settle. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes
- Expected Immunefi impact: High: exploitable cached misvaluation on a live integration bank
- Fast validation: Construct a boundary state for the derivative/integration asset and assert the accepted action remains safe when value is recomputed from the canonical settlement basis. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
