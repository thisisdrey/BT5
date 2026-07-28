# Q2109: Vault async deposit flow: multi-request / counter divergence / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with multiple pending deposit requests for the same controller before any claim while a holdings or ledger change zeroes `lastNavUpdate` between request and the next approval cycle and make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Unintended or unfair fund distribution through excess vault-share minting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: multiple pending deposit requests for the same controller before any claim
- Exploit idea: make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Unintended or unfair fund distribution through excess vault-share minting
- Fast validation: Assert that direct operator churn around request and claim never changes who can consume a controller's approved value.
