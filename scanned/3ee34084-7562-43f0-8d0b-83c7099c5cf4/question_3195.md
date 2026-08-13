# Q3195: lending_account_borrow: balance-slot reuse breaks per-bank accounting [a-user-near-initial-health] [cache-order]

## Question
Can an unprivileged attacker trigger `lending_account_borrow` with a user near initial-health failure under one price/collateral view so `lending_account_borrow` reuses, closes, or reopens a balance slot in a way that violates `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and causes `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user near initial-health failure under one price/collateral view
- Exploit idea: Look for state-machine edges where active/inactive balance bookkeeping can be confused around zeroing, reopening, or migration. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Exercise close-to-zero, close, reopen, and reuse sequences for the same user and assert no duplicate or orphaned exposure appears. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
