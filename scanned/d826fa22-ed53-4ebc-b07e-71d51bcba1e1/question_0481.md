# Q0481: Loans funding and disbursement state: lock cycle / wrong investor pull / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a normal list-lock-unlock cycle around the same loan before funding or disbursement while the NFT has been approved, listed, cancelled, or transferred shortly before the funding call and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Theft or unauthorized pull of investor USDC?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a normal list-lock-unlock cycle around the same loan before funding or disbursement
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Theft or unauthorized pull of investor USDC
- Fast validation: Forge test ownership changes, lock cycles, and standing allowances around `fund`, then assert only the current NFT owner can move capital exactly once.
