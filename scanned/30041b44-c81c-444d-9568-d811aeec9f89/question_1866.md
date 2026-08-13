# Q1866: check_primary_oracle_key: confidence or freshness enforcement is applied inconsistently [a-price-cache-update-attempt] [adapter-mismatch]

## Question
Can an unprivileged attacker reach `check_primary_oracle_key` through `lending_pool_pulse_bank_price_cache` with a price-cache update attempt after bank config was changed in another valid scenario so confidence/freshness checks are enforced on one branch but not the branch that settles value, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a price-cache update attempt after bank config was changed in another valid scenario
- Exploit idea: Check whether all price-consuming branches use the same freshness and confidence policy before accepting state transitions. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Feed boundary-age or high-confidence-spread contexts and assert no branch can accept if the canonical policy would reject. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
