# Q1655: get_price_of_type: stake or integration NAV path drifts from settled accounting [a-user-whose-health-barely] [freshness]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a user whose health barely changes under spot vs conservative price type so `get_price_of_type` values stake/integration exposure from a path that drifts from later settled accounting, breaking `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user whose health barely changes under spot vs conservative price type
- Exploit idea: Inspect special valuation helpers where derivative asset value is inferred indirectly and must still stay consistent with how deposits/withdrawals settle. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Construct a boundary state for the derivative/integration asset and assert the accepted action remains safe when value is recomputed from the canonical settlement basis. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
