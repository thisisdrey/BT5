# Q0324: Loans funding and disbursement state: standing allowance / wrong investor pull / same-loan accounting

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor and leading to Accounting issue in Loans that later affects withdrawals or vault pricing?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor
- Expected Immunefi impact: Accounting issue in Loans that later affects withdrawals or vault pricing
- Fast validation: Model a create-transfer-fund-disburse sequence and assert stale ownership or stale lock state never authorizes a later epoch.
