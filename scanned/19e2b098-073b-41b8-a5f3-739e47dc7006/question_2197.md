# Q2197: Vault async deposit flow: repeat approvals / dust strand / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with a controller that is approved in several partial chunks across time while the manager approves only part of the pending assets before later approvals and strand non-zero approved assets or shares that can no longer be claimed or cancelled, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Accounting issue in the vault leading to underbacked claimable shares?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: a controller that is approved in several partial chunks across time
- Exploit idea: strand non-zero approved assets or shares that can no longer be claimed or cancelled
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claimable shares
- Fast validation: Forge test two requests plus multiple partial approvals, then claim in alternating asset and share units and assert counter conservation.
