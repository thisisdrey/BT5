# Q1869: check_primary_oracle_key: confidence or freshness enforcement is applied inconsistently [a-bank-using-a-derivative] [freshness]

## Question
Can an unprivileged attacker reach `check_primary_oracle_key` through `lending_pool_pulse_bank_price_cache` with a bank using a derivative or integration-backed price adapter so confidence/freshness checks are enforced on one branch but not the branch that settles value, violating `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank using a derivative or integration-backed price adapter
- Exploit idea: Check whether all price-consuming branches use the same freshness and confidence policy before accepting state transitions. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Feed boundary-age or high-confidence-spread contexts and assert no branch can accept if the canonical policy would reject. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
