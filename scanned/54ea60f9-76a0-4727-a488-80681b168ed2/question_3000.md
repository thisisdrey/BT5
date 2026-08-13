# Q3000: lending_account_withdraw: repeatable cycle amplifies tiny accounting drift [a-withdraw-when-account-health] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_withdraw` under a withdraw when account health barely remains positive so `lending_account_withdraw` leaks value through a cycle that is individually small but cumulatively breaks `withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency` and leads to `Critical: direct theft or creation of bad debt via over-withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/withdraw.rs` / `lending_account_withdraw`
- Entrypoint: `lending_account_withdraw`
- Attacker controls: a withdraw when account health barely remains positive
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: withdraw must release only value actually owned by the caller while preserving fresh post-withdraw health and bank solvency
- Expected Immunefi impact: Critical: direct theft or creation of bad debt via over-withdrawal
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
