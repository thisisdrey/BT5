# Q499: change_liability_shares: same-bank aliasing across mutable balance updates [a-same-transaction-borrow-then] [cache-order]

## Question
Can an unprivileged attacker call `lending_account_repay` with a same-transaction borrow-then-repay sequence with boundary-sized values so that `change_liability_shares` mutates the same logical bank exposure through aliased or reused balance state, violating `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and causing `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a same-transaction borrow-then-repay sequence with boundary-sized values
- Exploit idea: Try to make a single user action touch one economic exposure twice through reused balance slots, duplicate remaining accounts, or stale active-balance bookkeeping. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Craft a test that reuses the same bank/account relationship in the controlled way and compare pre/post totals, shares, and user equity for double application. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
