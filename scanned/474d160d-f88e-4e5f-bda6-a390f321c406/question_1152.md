# Q1152: Investor withdrawal routing: lock churn / stuck route / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with lock, unlock, listing, settlement, or cancellation transitions around the same loan set while a vault, exchange, or later counterparty relies on the same payable balances after the batch and make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Accounting issue in Loans that later misprices a vault or secondary sale?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: lock, unlock, listing, settlement, or cancellation transitions around the same loan set
- Exploit idea: make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Accounting issue in Loans that later misprices a vault or secondary sale
- Fast validation: Check that every withdrawable principal and interest balance can be claimed exactly once and never becomes unreachable after a normal epoch change.
