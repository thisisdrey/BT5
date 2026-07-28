# Q1121: Investor withdrawal routing: lock churn / wrong recipient / per-loan recipient

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with lock, unlock, listing, settlement, or cancellation transitions around the same loan set while a sale offer was recently accepted or cancelled before the batch executes and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state and leading to Theft or diversion of other users' loan cashflows?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: lock, unlock, listing, settlement, or cancellation transitions around the same loan set
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state
- Expected Immunefi impact: Theft or diversion of other users' loan cashflows
- Fast validation: Forge test mixed locked/unlocked batches, duplicate ids, and ownership transfers, then assert each loan pays exactly one valid recipient once.
