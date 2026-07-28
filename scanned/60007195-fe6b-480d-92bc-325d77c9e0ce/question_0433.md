# Q0433: Loans funding and disbursement state: fresh owner / wrong investor pull / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan NFT that the attacker just acquired through a normal transfer or exchange settlement while a vault, buyer, or downstream counterparty could rely on the resulting balances after funding or disbursement and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Theft or unauthorized pull of investor USDC?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan NFT that the attacker just acquired through a normal transfer or exchange settlement
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Theft or unauthorized pull of investor USDC
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
