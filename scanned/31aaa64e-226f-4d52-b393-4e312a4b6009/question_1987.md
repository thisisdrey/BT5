# Q1987: load_kamino_reserve: confidence or freshness enforcement is applied inconsistently [a-reserve-refresh-context-that] [freshness]

## Question
Can an unprivileged attacker reach `load_kamino_reserve` through `lending_pool_pulse_bank_price_cache` with a reserve refresh context that differs from the reserve later valued so confidence/freshness checks are enforced on one branch but not the branch that settles value, violating `external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes` and causing `High: exploitable cached misvaluation on a live integration bank`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `load_kamino_reserve`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a reserve refresh context that differs from the reserve later valued
- Exploit idea: Check whether all price-consuming branches use the same freshness and confidence policy before accepting state transitions. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes
- Expected Immunefi impact: High: exploitable cached misvaluation on a live integration bank
- Fast validation: Feed boundary-age or high-confidence-spread contexts and assert no branch can accept if the canonical policy would reject. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
