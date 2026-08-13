# Q3303: lending_account_repay: frozen or disabled account still reaches value-moving code [a-repay-after-permissionless-interest] [cache-order]

## Question
Can an unprivileged attacker route `lending_account_repay` through `lending_account_repay` with a repay after permissionless interest accrual changed bank totals so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment` and causing `High: understated debt enabling later unauthorized withdrawal or protocol loss`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/instructions/marginfi_account/repay.rs` / `lending_account_repay`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay after permissionless interest accrual changed bank totals
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay must burn exactly the debt it settles and cannot erase liabilities without equivalent economic repayment
- Expected Immunefi impact: High: understated debt enabling later unauthorized withdrawal or protocol loss
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
