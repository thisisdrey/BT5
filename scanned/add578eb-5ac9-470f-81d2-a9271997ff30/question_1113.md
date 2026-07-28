# Q1113: Investor withdrawal routing: lock churn / batch bleed / per-loan recipient

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with lock, unlock, listing, settlement, or cancellation transitions around the same loan set while the first loan in the batch is locked and fixes the recipient as the active unlocker and make the authorization or recipient cached from the first loan bleed into another loan that should not share it, breaking the rule that every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state and leading to Loans NFT cashflow rights becoming stuck or routed to the wrong party?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: lock, unlock, listing, settlement, or cancellation transitions around the same loan set
- Exploit idea: make the authorization or recipient cached from the first loan bleed into another loan that should not share it
- Invariant to test: every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state
- Expected Immunefi impact: Loans NFT cashflow rights becoming stuck or routed to the wrong party
- Fast validation: Model sale-offer settlement or cancellation around `investorWithdraw` and assert no stale unlocker or stale owner can claim old payables.
