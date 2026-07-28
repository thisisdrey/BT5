# Q0515: Loans funding and disbursement state: role handoff / wrong investor pull / no stale owner

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan whose investor ownership changes between creation and the attempted funding call while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch and leading to Bypass of intended permissions and lifecycle guards around funding or disbursement?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan whose investor ownership changes between creation and the attempted funding call
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch
- Expected Immunefi impact: Bypass of intended permissions and lifecycle guards around funding or disbursement
- Fast validation: Assert that any failed disbursement or replay attempt reverts without leaving priceable or withdrawable balances behind.
