# Q3063: lending_account_borrow: same-bank aliasing across mutable balance updates [an-account-with-cross-bank] [cache-order]

## Question
Can an unprivileged attacker call `lending_account_borrow` with an account with cross-bank exposures that change mode eligibility so that `lending_account_borrow` mutates the same logical bank exposure through aliased or reused balance state, violating `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and causing `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: an account with cross-bank exposures that change mode eligibility
- Exploit idea: Try to make a single user action touch one economic exposure twice through reused balance slots, duplicate remaining accounts, or stale active-balance bookkeeping. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Craft a test that reuses the same bank/account relationship in the controlled way and compare pre/post totals, shares, and user equity for double application. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
