# Q0523: Loans funding and disbursement state: role handoff / phase skip / no stale owner

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan whose investor ownership changes between creation and the attempted funding call while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable, breaking the rule that a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch and leading to Accounting issue in Loans that later affects withdrawals or vault pricing?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan whose investor ownership changes between creation and the attempted funding call
- Exploit idea: make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable
- Invariant to test: a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch
- Expected Immunefi impact: Accounting issue in Loans that later affects withdrawals or vault pricing
- Fast validation: Model a create-transfer-fund-disburse sequence and assert stale ownership or stale lock state never authorizes a later epoch.
