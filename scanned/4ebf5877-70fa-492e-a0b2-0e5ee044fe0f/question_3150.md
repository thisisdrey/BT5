# Q3150: lending_account_borrow: frozen or disabled account still reaches value-moving code [a-repeated-borrow-repay-cycle] [cycle]

## Question
Can an unprivileged attacker route `lending_account_borrow` through `lending_account_borrow` with a repeated borrow/repay cycle with tiny amount asymmetry so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and causing `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a repeated borrow/repay cycle with tiny amount asymmetry
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
