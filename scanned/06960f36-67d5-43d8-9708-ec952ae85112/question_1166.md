# Q1166: Investor withdrawal routing: mixed balances / stuck route / single claim per payable

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with a batch where some loans have only principal, some only interest, and some zero withdrawable value while the first loan in the batch is unlocked and fixes the recipient as the current investor and make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary, breaking the rule that each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs and leading to Accounting issue in Loans that later misprices a vault or secondary sale?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: a batch where some loans have only principal, some only interest, and some zero withdrawable value
- Exploit idea: make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary
- Invariant to test: each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs
- Expected Immunefi impact: Accounting issue in Loans that later misprices a vault or secondary sale
- Fast validation: Model sale-offer settlement or cancellation around `investorWithdraw` and assert no stale unlocker or stale owner can claim old payables.
