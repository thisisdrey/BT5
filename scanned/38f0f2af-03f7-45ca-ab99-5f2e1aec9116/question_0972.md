# Q0972: Investor withdrawal routing: duplicate ids / batch bleed / no stale unlocker

## Question
Can an unprivileged investor, unlocker, buyer, or seller using only normal withdrawal and transfer flows enter through `Loans.investorWithdraw(uint64[],uint48,bytes32)` with duplicate, reordered, or adversarially mixed loan ids in the batch while the first loan in the batch is unlocked and fixes the recipient as the current investor and make the authorization or recipient cached from the first loan bleed into another loan that should not share it, breaking the rule that a cleared or changed lock should never preserve withdrawal rights into the next epoch and leading to Loans NFT cashflow rights becoming stuck or routed to the wrong party?

## Target
- File/function: contracts/Loans.sol / investorWithdraw
- Entrypoint: Loans.investorWithdraw(uint64[],uint48,bytes32)
- Attacker controls: duplicate, reordered, or adversarially mixed loan ids in the batch
- Exploit idea: make the authorization or recipient cached from the first loan bleed into another loan that should not share it
- Invariant to test: a cleared or changed lock should never preserve withdrawal rights into the next epoch
- Expected Immunefi impact: Loans NFT cashflow rights becoming stuck or routed to the wrong party
- Fast validation: Check that every withdrawable principal and interest balance can be claimed exactly once and never becomes unreachable after a normal epoch change.
