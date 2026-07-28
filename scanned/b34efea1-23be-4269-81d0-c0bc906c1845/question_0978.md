# Q0978: Investor withdrawal routing: duplicate ids / wrong recipient / single claim per payable

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with duplicate, reordered, or adversarially mixed loan ids in the batch while the first loan in the batch is locked and fixes the recipient as the active unlocker and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: duplicate, reordered, or adversarially mixed loan ids in the batch
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Forge test mixed locked/unlocked batches, duplicate ids, and ownership transfers, then assert each loan pays exactly one valid recipient once.
