# Q1237: Investor withdrawal routing: epoch split / double claim / per-loan recipient

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker while the first loan in the batch is locked and fixes the recipient as the active unlocker and make one payable balance claimable in two ownership or lock epochs, breaking the rule that every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker
- Exploit idea: make one payable balance claimable in two ownership or lock epochs
- Invariant to test: every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Forge test mixed locked/unlocked batches, duplicate ids, and ownership transfers, then assert each loan pays exactly one valid recipient once.
