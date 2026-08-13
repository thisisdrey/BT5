# Q3137: lending_account_borrow: frozen or disabled account still reaches value-moving code [a-borrow-amount-at-the] [cache-order]

## Question
Can an unprivileged attacker route `lending_account_borrow` through `lending_account_borrow` with a borrow amount at the exact borrow-cap boundary so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and causing `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow amount at the exact borrow-cap boundary
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
