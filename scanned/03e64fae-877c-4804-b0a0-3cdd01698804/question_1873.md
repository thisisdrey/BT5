# Q1873: check_primary_oracle_key: price-type routing picks a safer-looking but wrong value [remaining-accounts-containing-a-valid] [freshness]

## Question
Can an unprivileged attacker use `lending_pool_pulse_bank_price_cache` with remaining accounts containing a valid-looking but foreign primary price source so `check_primary_oracle_key` routes to the wrong price type for the mutation being performed, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and leading to `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: remaining accounts containing a valid-looking but foreign primary price source
- Exploit idea: Audit distinctions like spot vs TWAP vs cache vs confidence-ignored pricing to ensure each value-moving path uses the intended conservative source. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Exercise paths where price-type choice matters economically and assert the selected type matches protocol design for that exact action. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
