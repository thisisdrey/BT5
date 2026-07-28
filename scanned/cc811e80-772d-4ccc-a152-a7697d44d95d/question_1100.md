# Q1100: Investor withdrawal routing: lock churn / batch bleed / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with lock, unlock, listing, settlement, or cancellation transitions around the same loan set while the first loan in the batch is unlocked and fixes the recipient as the current investor and make the authorization or recipient cached from the first loan bleed into another loan that should not share it, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Accounting issue in Loans that later misprices a vault or secondary sale?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: lock, unlock, listing, settlement, or cancellation transitions around the same loan set
- Exploit idea: make the authorization or recipient cached from the first loan bleed into another loan that should not share it
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Accounting issue in Loans that later misprices a vault or secondary sale
- Fast validation: Model sale-offer settlement or cancellation around `investorWithdraw` and assert no stale unlocker or stale owner can claim old payables.
