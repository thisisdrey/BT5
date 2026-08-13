# Q1692: get_price_of_type: price cache update enables later unauthorized value extraction [a-borrow-where-omitted-optional] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a borrow where omitted optional accounts change the price-loading branch so `get_price_of_type` commits a misleading but accepted cache update that later breaks `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and leads to `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow where omitted optional accounts change the price-loading branch
- Exploit idea: Audit permissionless price-cache writes for any caller-controlled dimensions that later trusted instructions consume without recomputing. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Write the controlled cache state, then immediately invoke dependent instructions and assert none can extract value unless a fresh canonical price agrees. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
