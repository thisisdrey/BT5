# Q1673: get_price_of_type: caller-chosen remaining accounts suppress a required price check [an-account-with-multiple-banks] [freshness]

## Question
Can an unprivileged attacker use `lending_account_borrow` with an account with multiple banks relying on different price modes so `get_price_of_type` skips a required price validation branch because the caller shaped remaining accounts, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: an account with multiple banks relying on different price modes
- Exploit idea: Look for optional-account or path-selection behavior where omitted accounts change the safety checks applied. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Omit or reorder the controlled accounts and assert any path that mutates value still enforces the same canonical price checks. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
