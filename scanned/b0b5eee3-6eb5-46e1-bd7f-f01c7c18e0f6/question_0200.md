# Q200: get_liability_shares: rounding boundary creates extractable dust [a-user-that-toggles-into] [cycle]

## Question
Can an unprivileged attacker use `lending_account_borrow` with a user that toggles into or out of eMode just before borrowing to push `get_liability_shares` across a rounding edge where protocol totals and user shares no longer match, breaking `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and eventually causing `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a user that toggles into or out of eMode just before borrowing
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
