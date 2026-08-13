# Q1907: check_primary_oracle_key: stake or integration NAV path drifts from settled accounting [a-bank-with-multiple-historical] [freshness]

## Question
Can an unprivileged attacker use `lending_pool_pulse_bank_price_cache` with a bank with multiple historical or sibling oracle contexts available on-chain so `check_primary_oracle_key` values stake/integration exposure from a path that drifts from later settled accounting, breaking `permissionless cache refresh must only ever bind the bank to its exact configured primary price source` and causing `High: exploitable cached mispricing that later enables theft or insolvency`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `check_primary_oracle_key`
- Entrypoint: `lending_pool_pulse_bank_price_cache`
- Attacker controls: a bank with multiple historical or sibling oracle contexts available on-chain
- Exploit idea: Inspect special valuation helpers where derivative asset value is inferred indirectly and must still stay consistent with how deposits/withdrawals settle. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: permissionless cache refresh must only ever bind the bank to its exact configured primary price source
- Expected Immunefi impact: High: exploitable cached mispricing that later enables theft or insolvency
- Fast validation: Construct a boundary state for the derivative/integration asset and assert the accepted action remains safe when value is recomputed from the canonical settlement basis. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
