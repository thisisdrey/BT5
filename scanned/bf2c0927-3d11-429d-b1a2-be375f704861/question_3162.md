# Q3162: lending_account_borrow: repeatable cycle amplifies tiny accounting drift [a-borrow-immediately-after-permissionless] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_borrow` under a borrow immediately after permissionless cache or interest maintenance so `lending_account_borrow` leaks value through a cycle that is individually small but cumulatively breaks `borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral` and leads to `Critical: unbacked debt and protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/borrow.rs` / `lending_account_borrow`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a borrow immediately after permissionless cache or interest maintenance
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrow must use a fresh conservative risk view and cannot mint liabilities that understate debt or overstate collateral
- Expected Immunefi impact: Critical: unbacked debt and protocol insolvency
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
