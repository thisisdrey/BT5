# Q2204: Vault async deposit flow: repeat approvals / price-lock break / no stranded claim

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with a controller that is approved in several partial chunks across time while the manager approves only part of the pending assets before later approvals and make the shares claimed exceed the approval-time price lock for the same controller, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value and leading to Accounting issue in the vault leading to underbacked claimable shares?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: a controller that is approved in several partial chunks across time
- Exploit idea: make the shares claimed exceed the approval-time price lock for the same controller
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claimable shares
- Fast validation: Forge test two requests plus multiple partial approvals, then claim in alternating asset and share units and assert counter conservation.
