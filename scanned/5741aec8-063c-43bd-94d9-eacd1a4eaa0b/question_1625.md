# Q1625: get_price_of_type: price-type routing picks a safer-looking but wrong value [an-account-with-multiple-banks] [freshness]

## Question
Can an unprivileged attacker use `lending_account_borrow` with an account with multiple banks relying on different price modes so `get_price_of_type` routes to the wrong price type for the mutation being performed, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and leading to `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: an account with multiple banks relying on different price modes
- Exploit idea: Audit distinctions like spot vs TWAP vs cache vs confidence-ignored pricing to ensure each value-moving path uses the intended conservative source. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Exercise paths where price-type choice matters economically and assert the selected type matches protocol design for that exact action. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
