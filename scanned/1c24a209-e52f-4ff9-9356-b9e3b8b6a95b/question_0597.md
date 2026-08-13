# Q597: change_liability_shares: repeatable cycle amplifies tiny accounting drift [remaining-accounts-that-include-multiple] [cache-order]

## Question
Can an unprivileged attacker repeat `lending_account_repay` under remaining accounts that include multiple liabilities for the same user so `change_liability_shares` leaks value through a cycle that is individually small but cumulatively breaks `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and leads to `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: remaining accounts that include multiple liabilities for the same user
- Exploit idea: Look for a per-call mismatch that can be looped cheaply without relying on heavy traffic, especially deposit/withdraw, borrow/repay, or accrue/settle cycles. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Run a deterministic loop of the controlled sequence and assert the attacker cannot monotonically increase assets, reduce liabilities, or move protocol totals. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
