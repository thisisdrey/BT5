# Q2156: Vault async deposit flow: partial claim / price-lock break / no stranded claim

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with alternating deposit() and mint() claims against the same claimable position while the controller already has a prior claimable deposit or pending redeem position and make the shares claimed exceed the approval-time price lock for the same controller, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value and leading to Accounting issue in the vault leading to underbacked claimable shares?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: alternating deposit() and mint() claims against the same claimable position
- Exploit idea: make the shares claimed exceed the approval-time price lock for the same controller
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claimable shares
- Fast validation: Assert that direct operator churn around request and claim never changes who can consume a controller's approved value.
