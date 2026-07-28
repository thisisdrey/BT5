# Q1136: Investor withdrawal routing: lock churn / stuck route / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with lock, unlock, listing, settlement, or cancellation transitions around the same loan set while a sale offer was recently accepted or cancelled before the batch executes and make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Unintended or unfair fund distribution across investors, buyers, or sellers?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: lock, unlock, listing, settlement, or cancellation transitions around the same loan set
- Exploit idea: make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Unintended or unfair fund distribution across investors, buyers, or sellers
- Fast validation: Fuzz batched loan ordering and payable compositions and assert the first loan's cached recipient never bleeds into another loan improperly.
