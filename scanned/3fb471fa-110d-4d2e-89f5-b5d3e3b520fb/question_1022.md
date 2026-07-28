# Q1022: Investor withdrawal routing: duplicate ids / stuck route / single claim per payable

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with duplicate, reordered, or adversarially mixed loan ids in the batch while a vault, exchange, or later counterparty relies on the same payable balances after the batch and make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary, breaking the rule that each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs and leading to Accounting issue in Loans that later misprices a vault or secondary sale?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: duplicate, reordered, or adversarially mixed loan ids in the batch
- Exploit idea: make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary
- Invariant to test: each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs
- Expected Immunefi impact: Accounting issue in Loans that later misprices a vault or secondary sale
- Fast validation: Check that every withdrawable principal and interest balance can be claimed exactly once and never becomes unreachable after a normal epoch change.
