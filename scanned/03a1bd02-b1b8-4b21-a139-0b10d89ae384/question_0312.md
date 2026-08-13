# Q312: get_liability_shares: balance-slot reuse breaks per-bank accounting [a-user-that-toggles-into] [cycle]

## Question
Can an unprivileged attacker trigger `lending_account_borrow` with a user that toggles into or out of eMode just before borrowing so `get_liability_shares` reuses, closes, or reopens a balance slot in a way that violates `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and causes `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user that toggles into or out of eMode just before borrowing
- Exploit idea: Look for state-machine edges where active/inactive balance bookkeeping can be confused around zeroing, reopening, or migration. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Exercise close-to-zero, close, reopen, and reuse sequences for the same user and assert no duplicate or orphaned exposure appears. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
