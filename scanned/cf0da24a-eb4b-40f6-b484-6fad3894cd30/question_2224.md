# Q2224: Vault async deposit flow: repeat approvals / counter divergence / no stranded claim

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with a controller that is approved in several partial chunks across time while the controller already has a prior claimable deposit or pending redeem position and make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value and leading to Unintended or unfair fund distribution through excess vault-share minting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: a controller that is approved in several partial chunks across time
- Exploit idea: make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value
- Expected Immunefi impact: Unintended or unfair fund distribution through excess vault-share minting
- Fast validation: Forge test two requests plus multiple partial approvals, then claim in alternating asset and share units and assert counter conservation.
