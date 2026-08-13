# Q1695: get_price_of_type: price cache update enables later unauthorized value extraction [a-state-where-derivative-or] [freshness]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a state where derivative or integration price helpers can load two plausible contexts so `get_price_of_type` commits a misleading but accepted cache update that later breaks `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and leads to `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a state where derivative or integration price helpers can load two plausible contexts
- Exploit idea: Audit permissionless price-cache writes for any caller-controlled dimensions that later trusted instructions consume without recomputing. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Write the controlled cache state, then immediately invoke dependent instructions and assert none can extract value unless a fresh canonical price agrees. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
