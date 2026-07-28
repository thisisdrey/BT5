# Q1071: Investor withdrawal routing: ownership churn / stuck route / batch isolation

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with NFT ownership changes across attacker-controlled addresses between withdrawal attempts while a sale offer was recently accepted or cancelled before the batch executes and make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary, breaking the rule that batch authorization and recipient caching should never merge entitlements across loans that only appear similar and leading to Theft or diversion of other users' loan cashflows?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: NFT ownership changes across attacker-controlled addresses between withdrawal attempts
- Exploit idea: make a valid payable balance become unreachable because lock or ownership state changed at the wrong boundary
- Invariant to test: batch authorization and recipient caching should never merge entitlements across loans that only appear similar
- Expected Immunefi impact: Theft or diversion of other users' loan cashflows
- Fast validation: Forge test mixed locked/unlocked batches, duplicate ids, and ownership transfers, then assert each loan pays exactly one valid recipient once.
