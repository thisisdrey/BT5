# Q0388: Loans funding and disbursement state: fresh owner / wrong investor pull / same-loan accounting

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan NFT that the attacker just acquired through a normal transfer or exchange settlement while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor and leading to Theft or unauthorized pull of investor USDC?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan NFT that the attacker just acquired through a normal transfer or exchange settlement
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: funding or disbursing one loan should never create withdrawable or priceable balances attributable to another loan or investor
- Expected Immunefi impact: Theft or unauthorized pull of investor USDC
- Fast validation: Forge test ownership changes, lock cycles, and standing allowances around `fund`, then assert only the current NFT owner can move capital exactly once.
