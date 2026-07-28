# Q0626: Loans funding and disbursement state: batch neighbor / wrong investor pull / exact commitment

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls while a vault, buyer, or downstream counterparty could rely on the resulting balances after funding or disbursement and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that the funded amount and disbursed amount should always map to the same remaining commitment for the same loan and leading to Theft or unauthorized pull of investor USDC?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: the funded amount and disbursed amount should always map to the same remaining commitment for the same loan
- Expected Immunefi impact: Theft or unauthorized pull of investor USDC
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
