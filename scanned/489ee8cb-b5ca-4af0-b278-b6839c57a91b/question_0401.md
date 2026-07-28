# Q0401: Loans funding and disbursement state: fresh owner / wrong investor pull / single funding

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a loan NFT that the attacker just acquired through a normal transfer or exchange settlement while status-like fields or off-chain expectations changed nearby but the economic balances still define the real state and make `fund` pull from or account for the wrong investor identity for the same loan, breaking the rule that `fund` should only succeed once per loan and only for the current NFT owner of that same loan and leading to Unintended or unfair fund distribution between investor, borrower, and originator paths?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a loan NFT that the attacker just acquired through a normal transfer or exchange settlement
- Exploit idea: make `fund` pull from or account for the wrong investor identity for the same loan
- Invariant to test: `fund` should only succeed once per loan and only for the current NFT owner of that same loan
- Expected Immunefi impact: Unintended or unfair fund distribution between investor, borrower, and originator paths
- Fast validation: Forge test ownership changes, lock cycles, and standing allowances around `fund`, then assert only the current NFT owner can move capital exactly once.
