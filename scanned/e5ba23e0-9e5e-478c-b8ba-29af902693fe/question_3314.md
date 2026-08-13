# Q3314: lending_account_repay: repeatable cycle amplifies tiny accounting drift [a-repay-amount-at-the] [cycle]

## Question
Can an unprivileged attacker repeat `lending_account_repay` under a repay amount at the last-liability-share boundary so `lending_account_repay` leaks value through a cycle that is individually small but cumulatively breaks `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and leads to `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay amount at the last-liability-share boundary
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether the attacker can repeat the same path in a short deterministic cycle to compound a one-step drift.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Run the target path in a short deposit/withdraw or borrow/repay style loop and assert no monotonic gain appears.
