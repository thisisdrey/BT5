# Q0353: Loans funding and disbursement state: standing allowance / wrong investor pull / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing while the NFT has been approved, listed, cancelled, or transferred shortly before the funding call and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
