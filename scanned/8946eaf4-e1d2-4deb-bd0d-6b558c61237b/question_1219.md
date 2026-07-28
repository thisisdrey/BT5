# Q1219: Investor withdrawal routing: epoch split / wrong recipient / batch isolation

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker while the first loan in the batch is unlocked and fixes the recipient as the current investor and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that batch authorization and recipient caching should never merge entitlements across loans that only appear similar and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: batch authorization and recipient caching should never merge entitlements across loans that only appear similar
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Fuzz batched loan ordering and payable compositions and assert the first loan's cached recipient never bleeds into another loan improperly.
