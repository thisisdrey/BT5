# Q278: get_liability_shares: repeatable cycle amplifies tiny accounting drift [a-same-slot-repay-then] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_borrow` under a same-slot repay-then-borrow sequence around the same bank so `get_liability_shares` leaks value through a cycle that is individually small but cumulatively breaks `borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity` and leads to `Critical: creation of unbacked debt or protocol insolvency`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `get_liability_shares`
- Entrypoint: `lending_account_borrow`
- Attacker controls: a same-slot repay-then-borrow sequence around the same bank
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: borrowed liabilities must always correspond to a conservative increase in repayable debt and never exceed fresh borrow capacity
- Expected Immunefi impact: Critical: creation of unbacked debt or protocol insolvency
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
