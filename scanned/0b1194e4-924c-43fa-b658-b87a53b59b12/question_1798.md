# Q1798: get_price_and_confidence_of_type: caller-chosen remaining accounts suppress a required price check [a-same-slot-cache-refresh] [adapter-mismatch]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with a same-slot cache refresh before liquidation so `get_price_and_confidence_of_type` skips a required price validation branch because the caller shaped remaining accounts, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a same-slot cache refresh before liquidation
- Exploit idea: Look for optional-account or path-selection behavior where omitted accounts change the safety checks applied. Focus specifically on integration-backed price adapters where reserve/market and oracle contexts can be cross-wired.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Omit or reorder the controlled accounts and assert any path that mutates value still enforces the same canonical price checks. Provide two plausible adapter contexts and assert the price path rejects every mix-and-match combination.
