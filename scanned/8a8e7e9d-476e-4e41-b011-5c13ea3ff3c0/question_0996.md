# Q0996: Investor withdrawal routing: duplicate ids / wrong recipient / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with duplicate, reordered, or adversarially mixed loan ids in the batch while a sale offer was recently accepted or cancelled before the batch executes and make principal or interest route to the wrong recipient for one of the batched loans, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Loans NFT cashflow rights becoming stuck or routed to the wrong party?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: duplicate, reordered, or adversarially mixed loan ids in the batch
- Exploit idea: make principal or interest route to the wrong recipient for one of the batched loans
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Loans NFT cashflow rights becoming stuck or routed to the wrong party
- Fast validation: Check that every withdrawable principal and interest balance can be claimed exactly once and never becomes unreachable after a normal epoch change.
