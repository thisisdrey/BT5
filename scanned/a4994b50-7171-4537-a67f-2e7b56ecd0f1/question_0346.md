# Q0346: Loans funding and disbursement state: standing allowance / phase skip / exact commitment

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing while status-like fields or off-chain expectations changed nearby but the economic balances still define the real state and make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable, breaking the rule that the funded amount and disbursed amount should always map to the same remaining commitment for the same loan and leading to Theft or unauthorized pull of investor USDC?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing
- Exploit idea: make disbursement succeed after an unprivileged sequence that should have left the loan unfundable or undisbursable
- Invariant to test: the funded amount and disbursed amount should always map to the same remaining commitment for the same loan
- Expected Immunefi impact: Theft or unauthorized pull of investor USDC
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
