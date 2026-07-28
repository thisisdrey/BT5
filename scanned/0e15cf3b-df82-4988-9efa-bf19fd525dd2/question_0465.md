# Q0465: Loans funding and disbursement state: lock cycle / wrong investor pull / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a normal list-lock-unlock cycle around the same loan before funding or disbursement while status-like fields or off-chain expectations changed nearby but the economic balances still define the real state and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Accounting issue in Loans that later affects withdrawals or vault pricing?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a normal list-lock-unlock cycle around the same loan before funding or disbursement
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Accounting issue in Loans that later affects withdrawals or vault pricing
- Fast validation: Assert that any failed disbursement or replay attempt reverts without leaving priceable or withdrawable balances behind.
