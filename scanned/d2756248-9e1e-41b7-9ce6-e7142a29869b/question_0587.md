# Q587: change_liability_shares: frozen or disabled account still reaches value-moving code [a-repay-where-another-balance] [cache-order]

## Question
Can an unprivileged attacker route `lending_account_repay` through `change_liability_shares` with a repay where another balance on the account becomes inactive in the same slot so a frozen, disabled, or otherwise blocked account still changes value-bearing state, breaking `repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants` and causing `High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal`? Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.

## Target
- File/function: `programs/marginfi/src/state/bank.rs` / `change_liability_shares`
- Entrypoint: `lending_account_repay`
- Attacker controls: a repay where another balance on the account becomes inactive in the same slot
- Exploit idea: Test whether authority/freeze/disabled checks are performed too late, on the wrong object, or on only part of the execution path. Focus specifically on whether one earlier public instruction in the same transaction can change a cache, flag, or active-balance view before settlement.
- Invariant to test: repay paths must reduce exactly the debt they settle and cannot erase more liability than the repaid value warrants
- Expected Immunefi impact: High: exploitable debt understatement that can lead to later insolvency or unauthorized withdrawal
- Fast validation: Set the relevant flags, execute the controlled call, and assert that no vault transfer, share change, or balance activation occurs. Add an adversarial same-transaction precursor that changes dependent cache or balance state before the target call.
