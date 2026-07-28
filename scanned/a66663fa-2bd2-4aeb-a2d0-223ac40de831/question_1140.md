# Q1140: Investor withdrawal routing: lock churn / wrong recipient / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with lock, unlock, listing, settlement, or cancellation transitions around the same loan set while a vault, exchange, or later counterparty relies on the same payable balances after the batch and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Theft or diversion of other users' loan cashflows?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: lock, unlock, listing, settlement, or cancellation transitions around the same loan set
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Theft or diversion of other users' loan cashflows
- Fast validation: Fuzz batched loan ordering and payable compositions and assert the first loan's cached recipient never bleeds into another loan improperly.
