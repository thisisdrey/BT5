# Q1584: get_price_of_type: oracle account selection changes the priced object [a-state-where-derivative-or] [adapter-mismatch]

## Question
Can an unprivileged attacker supply a state where derivative or integration price helpers can load two plausible contexts to `lending_account_borrow` so `get_price_of_type` prices the wrong object or wrong price source while mutating state for another, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a state where derivative or integration price helpers can load two plausible contexts
- Exploit idea: Attack remaining-account binding, oracle-key checks, and asset-tag routing so price lookup cannot be redirected by caller-controlled account order. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Provide multiple plausible oracle contexts and assert the path rejects unless the selected price source matches the validated bank configuration exactly. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
