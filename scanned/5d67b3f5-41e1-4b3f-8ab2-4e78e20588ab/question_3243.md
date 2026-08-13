# Q3243: lending_account_repay: rounding boundary creates extractable dust [a-repay-when-another-balance] [cache-order]

## Question
Can an unprivileged attacker use `lending_account_repay` with a repay when another balance is being zeroed simultaneously to push `lending_account_repay` across a rounding edge where protocol totals and user shares no longer match, breaking `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and eventually causing `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay when another balance is being zeroed simultaneously
- Exploit idea: Search for floor/ceil mismatches between user-facing token amounts and internal share accounting near zero, one-share, or threshold-sized transitions. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Fuzz tiny and boundary amounts around the relevant threshold and assert that repeated cycles cannot increase withdrawable assets or decrease repayable debt. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
