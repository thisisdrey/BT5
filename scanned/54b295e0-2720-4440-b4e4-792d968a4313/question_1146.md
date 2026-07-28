# Q1146: Investor withdrawal routing: lock churn / batch bleed / single claim per payable

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with lock, unlock, listing, settlement, or cancellation transitions around the same loan set while a vault, exchange, or later counterparty relies on the same payable balances after the batch and make the authorization or recipient cached from the first loan bleed into another loan that should not share it, breaking the rule that each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs and leading to Theft or diversion of other users' loan cashflows?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: lock, unlock, listing, settlement, or cancellation transitions around the same loan set
- Exploit idea: make the authorization or recipient cached from the first loan bleed into another loan that should not share it
- Invariant to test: each principal or interest payable balance should be withdrawable at most once across all ownership and lock epochs
- Expected Immunefi impact: Theft or diversion of other users' loan cashflows
- Fast validation: Fuzz batched loan ordering and payable compositions and assert the first loan's cached recipient never bleeds into another loan improperly.
