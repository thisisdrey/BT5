# Q1067: Investor withdrawal routing: ownership churn / batch bleed / batch isolation

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with NFT ownership changes across attacker-controlled addresses between withdrawal attempts while a sale offer was recently accepted or cancelled before the batch executes and make the authorization or recipient cached from the first loan bleed into another loan that should not share it, breaking the rule that batch authorization and recipient caching should never merge entitlements across loans that only appear similar and leading to Accounting issue in Loans that later misprices a vault or secondary sale?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: NFT ownership changes across attacker-controlled addresses between withdrawal attempts
- Exploit idea: make the authorization or recipient cached from the first loan bleed into another loan that should not share it
- Invariant to test: batch authorization and recipient caching should never merge entitlements across loans that only appear similar
- Expected Immunefi impact: Accounting issue in Loans that later misprices a vault or secondary sale
- Fast validation: Model sale-offer settlement or cancellation around `investorWithdraw` and assert no stale unlocker or stale owner can claim old payables.
