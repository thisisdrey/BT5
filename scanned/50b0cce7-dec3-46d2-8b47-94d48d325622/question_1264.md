# Q1264: Investor withdrawal routing: epoch split / stuck route / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker while a sale offer was recently accepted or cancelled before the batch executes and make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Theft or diversion of other users' loan cashflows?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: one loan from a fresh ownership epoch and one loan from a prior epoch that shares the same apparent investor or unlocker
- Exploit idea: make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Theft or diversion of other users' loan cashflows
- Fast validation: Forge test mixed locked/unlocked batches, duplicate ids, and ownership transfers, then assert each loan pays exactly one valid recipient once.
