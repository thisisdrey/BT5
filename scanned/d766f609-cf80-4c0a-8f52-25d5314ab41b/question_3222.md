# Q3222: lending_account_repay: same-bank aliasing across mutable balance updates [remaining-accounts-with-multiple-liabilities] [cycle]

## Question
Can an unprivileged attacker call `lending_account_repay` with remaining accounts with multiple liabilities and banks so that `lending_account_repay` mutates the same logical bank exposure through aliased or reused balance state, violating `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and causing `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: remaining accounts with multiple liabilities and banks
- Exploit idea: Try to make a single user action touch one economic exposure twice through reused balance slots, duplicate remaining accounts, or stale active-balance bookkeeping. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Craft a test that reuses the same bank/account relationship in the controlled way and compare pre/post totals, shares, and user equity for double application. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
