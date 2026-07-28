# Q2229: Vault async deposit flow: repeat approvals / dust strand / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with a controller that is approved in several partial chunks across time while a holdings or ledger change zeroes `lastNavUpdate` between request and the next approval cycle and strand non-zero approved assets or shares that can no longer be claimed or cancelled, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Unintended or unfair fund distribution through excess vault-share minting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: a controller that is approved in several partial chunks across time
- Exploit idea: strand non-zero approved assets or shares that can no longer be claimed or cancelled
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Unintended or unfair fund distribution through excess vault-share minting
- Fast validation: Assert that direct operator churn around request and claim never changes who can consume a controller's approved value.
