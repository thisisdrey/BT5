# Q1945: check_primary_oracle_key: price cache update enables later unauthorized value extraction [a-price-cache-update-attempt] [freshness]

## Question
Can an unprivileged attacker call `lending_pool_pulse_bank_price_cache` with a price-cache update attempt after bank config was changed in another valid scenario so `check_primary_oracle_key` commits a misleading but accepted cache update that later breaks `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and leads to `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a price-cache update attempt after bank config was changed in another valid scenario
- Exploit idea: Audit permissionless price-cache writes for any caller-controlled dimensions that later trusted instructions consume without recomputing. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Write the controlled cache state, then immediately invoke dependent instructions and assert none can extract value unless a fresh canonical price agrees. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
