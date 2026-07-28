# Q0622: Loans funding and disbursement state: batch neighbor / epoch confusion / exact commitment

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls while the NFT has been approved, listed, cancelled, or transferred shortly before the funding call and make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions, breaking the rule that the funded amount and disbursed amount should always map to the same remaining commitment for the same loan and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: one target loan plus a nearby loan in a different lifecycle phase that the attacker also controls
- Exploit idea: make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions
- Invariant to test: the funded amount and disbursed amount should always map to the same remaining commitment for the same loan
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
