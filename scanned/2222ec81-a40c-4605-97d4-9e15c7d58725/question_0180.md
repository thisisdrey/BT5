# Q180: get_liability_shares: same-bank aliasing across mutable balance updates [remaining-accounts-that-include-multiple] [cycle]

## Question
Can an unprivileged attacker call `lending_account_borrow` with remaining accounts that include multiple borrowable banks with valid-looking metadata so that `get_liability_shares` mutates the same logical bank exposure through aliased or reused balance state, violating `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and causing `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: remaining accounts that include multiple borrowable banks with valid-looking metadata
- Exploit idea: Try to make a single user action touch one economic exposure twice through reused balance slots, duplicate remaining accounts, or stale active-balance bookkeeping. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Craft a test that reuses the same bank/account relationship in the controlled way and compare pre/post totals, shares, and user equity for double application. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
