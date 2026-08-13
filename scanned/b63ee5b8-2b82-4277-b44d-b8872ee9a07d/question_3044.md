# Q3044: lending_account_borrow: share minting vs health check desync [remaining-accounts-that-allow-two] [cycle]

## Question
Can an unprivileged attacker enter through `lending_account_borrow` and make `lending_account_borrow` observe remaining accounts that allow two bank or price contexts to look plausible so that share minting/burning and health enforcement are evaluated from inconsistent state, breaking `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and leading to `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: remaining accounts that allow two bank or price contexts to look plausible
- Exploit idea: Drive pre-state checks and post-state share changes through a boundary case so the instruction accepts a state transition that should fail once all balances are recomputed consistently. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Build an integration test around `lending_account_borrow` with the controlled state, then assert that accepted execution leaves post-instruction health negative or value moved beyond the allowed amount. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
