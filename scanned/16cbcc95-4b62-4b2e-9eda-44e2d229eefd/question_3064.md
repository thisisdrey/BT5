# Q3064: lending_account_borrow: same-bank aliasing across mutable balance updates [an-account-with-cross-bank] [cycle]

## Question
Can an unprivileged attacker call `lending_account_borrow` with an account with cross-bank exposures that change mode eligibility so that `lending_account_borrow` mutates the same logical bank exposure through aliased or reused balance state, violating `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and causing `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: an account with cross-bank exposures that change mode eligibility
- Exploit idea: Try to make a single user action touch one economic exposure twice through reused balance slots, duplicate remaining accounts, or stale active-balance bookkeeping. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Craft a test that reuses the same bank/account relationship in the controlled way and compare pre/post totals, shares, and user equity for double application. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
