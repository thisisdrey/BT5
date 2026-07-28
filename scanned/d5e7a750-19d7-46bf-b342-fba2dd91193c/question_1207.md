# Q1207: Investor withdrawal routing: mixed balances / double claim / batch isolation

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with a batch where some loans have only principal, some only interest, and some zero withdrawable value while a vault, exchange, or later counterparty relies on the same payable balances after the batch and make one payable balance claimable in two ownership or lock epochs, breaking the rule that batch authorization and recipient caching should never merge entitlements across loans that only appear similar and leading to Loans NFT cashflow rights becoming stuck or routed to the wrong party?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: a batch where some loans have only principal, some only interest, and some zero withdrawable value
- Exploit idea: make one payable balance claimable in two ownership or lock epochs
- Invariant to test: batch authorization and recipient caching should never merge entitlements across loans that only appear similar
- Expected Immunefi impact: Loans NFT cashflow rights becoming stuck or routed to the wrong party
- Fast validation: Model sale-offer settlement or cancellation around `investorWithdraw` and assert no stale unlocker or stale owner can claim old payables.
