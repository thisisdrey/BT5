# Q3134: lending_account_borrow: cross-mode collateral view mismatch [a-repeated-borrow-repay-cycle] [cycle]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a repeated borrow/repay cycle with tiny amount asymmetry so `lending_account_borrow` evaluates account risk under one mode and settles value under another, violating `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and resulting in `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a repeated borrow/repay cycle with tiny amount asymmetry
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
