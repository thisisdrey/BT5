# Q2006: Vault async deposit flow: operator timing / dust strand / pending sum

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with an operator approval added and removed around request and claim boundaries while the manager approves only part of the pending assets before later approvals and strand non-zero approved assets or shares that can no longer be claimed or cancelled, breaking the rule that totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers and leading to Unintended or unfair fund distribution through excess vault-share minting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: an operator approval added and removed around request and claim boundaries
- Exploit idea: strand non-zero approved assets or shares that can no longer be claimed or cancelled
- Invariant to test: totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers
- Expected Immunefi impact: Unintended or unfair fund distribution through excess vault-share minting
- Fast validation: Assert that direct operator churn around request and claim never changes who can consume a controller's approved value.
