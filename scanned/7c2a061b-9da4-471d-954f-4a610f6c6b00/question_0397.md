# Q0397: Loans funding and disbursement state: fresh owner / epoch confusion / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan NFT that the attacker just acquired through a normal transfer or exchange settlement while the loan is still `Created` with positive borrower principal receivable and zero investor principal payable and make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Theft or unauthorized pull of investor USDC?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan NFT that the attacker just acquired through a normal transfer or exchange settlement
- Exploit idea: make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Theft or unauthorized pull of investor USDC
- Fast validation: Forge test ownership changes, lock cycles, and standing allowances around `fund`, then assert only the current NFT owner can move capital exactly once.
