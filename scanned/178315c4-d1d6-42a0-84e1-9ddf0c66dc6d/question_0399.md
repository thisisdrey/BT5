# Q0399: Loans funding and disbursement state: fresh owner / epoch confusion / no stale owner

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan NFT that the attacker just acquired through a normal transfer or exchange settlement while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions, breaking the rule that a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan NFT that the attacker just acquired through a normal transfer or exchange settlement
- Exploit idea: make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions
- Invariant to test: a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
