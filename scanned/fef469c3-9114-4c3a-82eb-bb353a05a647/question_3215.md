# Q3215: lending_account_repay: share minting vs health check desync [a-repay-combined-with-a] [cache-order]

## Question
Can an unprivileged attacker enter through `lending_account_repay` and make `lending_account_repay` observe a repay combined with a close-balance investigation path so that share minting/burning and health enforcement are evaluated from inconsistent state, breaking `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and leading to `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay combined with a close-balance investigation path
- Exploit idea: Drive pre-state checks and post-state share changes through a boundary case so the instruction accepts a state transition that should fail once all balances are recomputed consistently. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Build an integration test around `lending_account_repay` with the controlled state, then assert that accepted execution leaves post-instruction health negative or value moved beyond the allowed amount. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
