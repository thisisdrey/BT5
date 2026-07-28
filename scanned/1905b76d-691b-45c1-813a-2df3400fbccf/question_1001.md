# Q1001: Investor withdrawal routing: duplicate ids / batch bleed / per-loan recipient

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with duplicate, reordered, or adversarially mixed loan ids in the batch while a sale offer was recently accepted or cancelled before the batch executes and make the authorization or recipient cached from the first loan bleed into another loan that should not share it, breaking the rule that every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state and leading to Theft or diversion of other users' loan cashflows?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: duplicate, reordered, or adversarially mixed loan ids in the batch
- Exploit idea: make the authorization or recipient cached from the first loan bleed into another loan that should not share it
- Invariant to test: every loan in investorWithdraw should pay only the recipient implied by that same loan's current owner and lock state
- Expected Immunefi impact: Theft or diversion of other users' loan cashflows
- Fast validation: Forge test mixed locked/unlocked batches, duplicate ids, and ownership transfers, then assert each loan pays exactly one valid recipient once.
