# Q2029: load_kamino_reserve: integration price adapter accepts mismatched reserve/market context [a-pulse-where-the-group] [freshness]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a pulse where the group/bank binding is valid but external reserve lineage is not so `load_kamino_reserve` loads or refreshes pricing from a mismatched reserve/market context, violating `external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes` and causing `High: exploitable cached misvaluation on a live integration bank`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `load_kamino_reserve`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a pulse where the group/bank binding is valid but external reserve lineage is not
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes
- Expected Immunefi impact: High: exploitable cached misvaluation on a live integration bank
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
