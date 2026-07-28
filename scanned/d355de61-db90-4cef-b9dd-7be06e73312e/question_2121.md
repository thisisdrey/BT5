# Q2121: Vault async deposit flow: partial claim / price-lock break / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with alternating deposit() and mint() claims against the same claimable position while the manager approves while `lastNav` is fresh and non-zero and make the shares claimed exceed the approval-time price lock for the same controller, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to User funds stuck because approved deposits become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: alternating deposit() and mint() claims against the same claimable position
- Exploit idea: make the shares claimed exceed the approval-time price lock for the same controller
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: User funds stuck because approved deposits become partially unclaimable
- Fast validation: Inject an approval after a NAV invalidation cycle and assert the final claim never exceeds the exact approval-time price lock.
