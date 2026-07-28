# Q0383: Loans funding and disbursement state: standing allowance / epoch confusion / no stale owner

## Question
Can an unprivileged current investor, borrower, or ordinary caller trying to exploit funding or disbursement boundaries enter through `Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)` with a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing while a vault, buyer, or downstream counterparty could rely on the resulting balances after funding or disbursement and make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions, breaking the rule that a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch and leading to Theft or unauthorized pull of investor USDC?

## Target
- File/function: contracts/Loans.sol / fund -> disburse
- Entrypoint: Loans.fund(uint64,int128,uint48,bytes32) and Loans.disburse(...)
- Attacker controls: a standing ERC20 approval from the current investor plus attacker-controlled NFT transfer timing
- Exploit idea: make one ownership or lock epoch affect the next epoch so capital or rights move under stale assumptions
- Invariant to test: a stale owner, stale approval, or stale lock epoch should never authorize capital movement in a newer ownership epoch
- Expected Immunefi impact: Theft or unauthorized pull of investor USDC
- Fast validation: Fuzz commitment-sized amounts and lifecycle orderings, then assert funding and disbursement preserve the exact same commitment ledger.
