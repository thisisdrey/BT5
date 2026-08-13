# Q1795: get_price_and_confidence_of_type: caller-chosen remaining accounts suppress a required price check [a-victim-at-the-exact] [freshness]

## Question
Can an unprivileged attacker use `lending_account_liquidate` with a victim at the exact liquidatable boundary under one price view but not another so `get_price_and_confidence_of_type` skips a required price validation branch because the caller shaped remaining accounts, violating `liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch` and causing `High: over-liquidation, under-repayment, or protocol loss from misvaluation`? Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.

## Target
- File/function: `programs/marginfi/src/state/price.rs` / `get_price_and_confidence_of_type`
- Entrypoint: `lending_account_liquidate`
- Attacker controls: a victim at the exact liquidatable boundary under one price view but not another
- Exploit idea: Look for optional-account or path-selection behavior where omitted accounts change the safety checks applied. Focus specifically on freshness/confidence boundaries and whether the settling branch applies the same policy as the admitting branch.
- Invariant to test: liquidation pricing must use the exact configured source with consistent confidence/freshness enforcement on every branch
- Expected Immunefi impact: High: over-liquidation, under-repayment, or protocol loss from misvaluation
- Fast validation: Omit or reorder the controlled accounts and assert any path that mutates value still enforces the same canonical price checks. Build price contexts just inside and outside freshness or confidence limits and assert no mixed-policy acceptance occurs.
