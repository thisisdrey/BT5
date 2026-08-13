# Q527: change_liability_shares: rounding boundary creates extractable dust [a-repay-immediately-after-interest] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_repay` with a repay immediately after interest accrual for the bank but before any other user action to push `change_liability_shares` across a rounding edge where protocol totals and user shares no longer match, breaking `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and eventually causing `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay immediately after interest accrual for the bank but before any other user action
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
