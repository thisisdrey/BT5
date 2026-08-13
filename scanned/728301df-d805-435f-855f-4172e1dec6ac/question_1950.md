# Q1950: check_primary_oracle_key: price cache update enables later unauthorized value extraction [a-bank-using-a-derivative] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a bank using a derivative or integration-backed price adapter so `check_primary_oracle_key` commits a misleading but accepted cache update that later breaks `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and leads to `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank using a derivative or integration-backed price adapter
- Exploit idea: Audit permissionless price-cache writes for any caller-controlled dimensions that later trusted instructions consume without recomputing. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Write the controlled cache state, then immediately invoke dependent instructions and assert none can extract value unless a fresh canonical price agrees. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
