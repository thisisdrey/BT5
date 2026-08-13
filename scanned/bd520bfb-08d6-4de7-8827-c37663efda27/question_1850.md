# Q1850: check_primary_oracle_key: cached and live price paths disagree at a critical boundary [a-price-cache-update-attempt] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a price-cache update attempt after bank config was changed in another valid scenario so `check_primary_oracle_key` uses cached pricing in one place and live pricing in another, breaking `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a price-cache update attempt after bank config was changed in another valid scenario
- Exploit idea: Probe threshold states where a stale cached value would change borrowability, liquidatability, or settlement size versus a fresh lookup. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Prepare divergent cache/live prices and assert any accepted instruction remains valid under a single fresh pricing view. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
