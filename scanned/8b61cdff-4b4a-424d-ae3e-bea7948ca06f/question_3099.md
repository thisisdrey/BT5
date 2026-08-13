# Q3099: lending_account_borrow: remaining-accounts rebinding of the priced asset path [a-user-near-initial-health] [cache-order]

## Question
Can an unprivileged attacker supply a user near initial-health failure under one price/collateral view to `lending_account_borrow` so that `lending_account_borrow` binds the wrong priced asset, bank, or vault path during validation, violating `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and leading to `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user near initial-health failure under one price/collateral view
- Exploit idea: Attempt to swap, omit, or reorder remaining accounts so a valid-looking path executes against the wrong economic context without failing ownership/group checks. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Construct two active banks and intentionally permute remaining accounts, then assert the instruction cannot succeed against the wrong bank or oracle context. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
