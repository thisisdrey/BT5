# Q1156: Investor withdrawal routing: mixed balances / wrong recipient / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with a batch where some loans have only principal, some only interest, and some zero withdrawable value while the first loan in the batch is unlocked and fixes the recipient as the current investor and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: a batch where some loans have only principal, some only interest, and some zero withdrawable value
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Fuzz batched loan ordering and payable compositions and assert the first loan's cached recipient never bleeds into another loan improperly.
