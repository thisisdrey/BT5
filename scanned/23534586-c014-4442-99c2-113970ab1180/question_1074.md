# Q1074: Investor withdrawal routing: ownership churn / wrong recipient / single claim per payable

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with NFT ownership changes across attacker-controlled addresses between withdrawal attempts while a vault, exchange, or later counterparty relies on the same payable balances after the batch and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs and leading to Loans NFT cashflow rights becoming stuck or routed to the wrong party?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: NFT ownership changes across attacker-controlled addresses between withdrawal attempts
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs
- Expected Immunefi impact: Loans NFT cashflow rights becoming stuck or routed to the wrong party
- Fast validation: Model sale-offer settlement or cancellation around `investorWithdraw` and assert no stale unlocker or stale owner can claim old payables.
