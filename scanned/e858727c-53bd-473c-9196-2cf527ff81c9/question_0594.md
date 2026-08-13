# Q594: change_liability_shares: repeatable cycle amplifies tiny accounting drift [a-repay-amount-at-the] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_repay` under a repay amount at the last-share and zero-threshold boundary so `change_liability_shares` leaks value through a cycle that is individually small but cumulatively breaks `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and leads to `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay amount at the last-share and zero-threshold boundary
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
