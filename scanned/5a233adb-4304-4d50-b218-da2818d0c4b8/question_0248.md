# Q248: get_liability_shares: cross-mode collateral view mismatch [a-user-that-toggles-into] [cycle]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a user that toggles into or out of eMode just before borrowing so `get_liability_shares` evaluates account risk under one mode and settles value under another, violating `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and resulting in `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user that toggles into or out of eMode just before borrowing
- Exploit idea: Probe transitions involving eMode, isolated assets, or asset tags where one code path reads stale or differently weighted collateral than the mutating path settles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Create a user that changes the relevant mode/context around the call and assert the instruction cannot accept if recomputation under a single consistent mode would fail. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
