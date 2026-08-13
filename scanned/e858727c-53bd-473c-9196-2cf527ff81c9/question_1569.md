# Q1569: get_price_of_type: oracle account selection changes the priced object [remaining-accounts-containing-multiple-same] [freshness]

## Question
Can an unprivileged attacker supply remaining accounts containing multiple same-owner oracle-like accounts to `lending_account_borrow` so `get_price_of_type` prices the wrong object or wrong price source while mutating state for another, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: remaining accounts containing multiple same-owner oracle-like accounts
- Exploit idea: Attack remaining-account binding, oracle-key checks, and asset-tag routing so price lookup cannot be redirected by caller-controlled account order. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Provide multiple plausible oracle contexts and assert the path rejects unless the selected price source matches the validated bank configuration exactly. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
