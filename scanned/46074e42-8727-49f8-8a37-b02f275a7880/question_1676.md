# Q1676: get_price_of_type: caller-chosen remaining accounts suppress a required price check [a-borrow-where-omitted-optional] [adapter-mismatch]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a borrow where omitted optional accounts change the price-loading branch so `get_price_of_type` skips a required price validation branch because the caller shaped remaining accounts, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow where omitted optional accounts change the price-loading branch
- Exploit idea: Look for optional-account or path-selection behavior where omitted accounts change the safety checks applied. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Omit or reorder the controlled accounts and assert any path that mutates value still enforces the same canonical price checks. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
