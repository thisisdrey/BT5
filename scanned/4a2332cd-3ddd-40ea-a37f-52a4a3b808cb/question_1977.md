# Q1977: load_kamino_reserve: cached and live price paths disagree at a critical boundary [a-bank-with-derivative-pricing] [freshness]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a bank with derivative pricing whose reserve and oracle can be cross-wired so `load_kamino_reserve` uses cached pricing in one place and live pricing in another, breaking `external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes` and causing `High: exploitable cached misvaluation on a live integration bank`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `load_kamino_reserve`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank with derivative pricing whose reserve and oracle can be cross-wired
- Exploit idea: Probe threshold states where a stale cached value would change borrowability, liquidatability, or settlement size versus a fresh lookup. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: external reserve or market state loaded for pricing must be exactly the bank-configured one and fresh enough for safe cache writes
- Expected Immunefi impact: High: exploitable cached misvaluation on a live integration bank
- Fast validation: Prepare divergent cache/live prices and assert any accepted instruction remains valid under a single fresh pricing view. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
