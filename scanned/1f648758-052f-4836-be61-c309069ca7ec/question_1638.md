# Q1638: get_price_of_type: integration price adapter accepts mismatched reserve/market context [a-borrow-immediately-after-a] [adapter-mismatch]

## Question
Can an unprivileged attacker call `lending_account_borrow` with a borrow immediately after a permissionless price-cache pulse so `get_price_of_type` loads or refreshes pricing from a mismatched reserve/market context, violating `the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type` and causing `Critical: unbacked borrowing or solvency loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_of_type`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow immediately after a permissionless price-cache pulse
- Exploit idea: Probe helper functions that load external reserve/market state via remaining accounts and must bind them to the bank being mutated. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: the priced value used to admit borrowing must come from the exact configured price source and intended conservative price type
- Expected Immunefi impact: Critical: unbacked borrowing or solvency loss from misvaluation
- Fast validation: Mix two similar external contexts and assert the pricing adapter cannot succeed unless the exact configured context is supplied. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
