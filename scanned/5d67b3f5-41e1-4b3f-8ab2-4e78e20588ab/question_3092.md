# Q3092: lending_account_borrow: remaining-accounts rebinding of the priced asset path [remaining-accounts-that-allow-two] [cycle]

## Question
Can an unprivileged attacker supply remaining accounts that allow two bank or price contexts to look plausible to `lending_account_borrow` so that `lending_account_borrow` binds the wrong priced asset, bank, or vault path during validation, violating `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and leading to `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: remaining accounts that allow two bank or price contexts to look plausible
- Exploit idea: Attempt to swap, omit, or reorder remaining accounts so a valid-looking path executes against the wrong economic context without failing ownership/group checks. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Construct two active banks and intentionally permute remaining accounts, then assert the instruction cannot succeed against the wrong bank or oracle context. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
