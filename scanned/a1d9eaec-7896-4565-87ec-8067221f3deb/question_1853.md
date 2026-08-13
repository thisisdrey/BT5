# Q1853: check_primary_oracle_key: cached and live price paths disagree at a critical boundary [a-bank-using-a-derivative] [freshness]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a bank using a derivative or integration-backed price adapter so `check_primary_oracle_key` uses cached pricing in one place and live pricing in another, breaking `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank using a derivative or integration-backed price adapter
- Exploit idea: Probe threshold states where a stale cached value would change borrowability, liquidatability, or settlement size versus a fresh lookup. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Prepare divergent cache/live prices and assert any accepted instruction remains valid under a single fresh pricing view. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
