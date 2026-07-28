# Q2081: Vault async deposit flow: multi-request / ratio drift / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with multiple pending deposit requests for the same controller before any claim while the controller already has a prior claimable deposit or pending redeem position and make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Unintended or unfair fund distribution through excess vault-share minting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: multiple pending deposit requests for the same controller before any claim
- Exploit idea: make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Unintended or unfair fund distribution through excess vault-share minting
- Fast validation: Forge test two requests plus multiple partial approvals, then claim in alternating asset and share units and assert counter conservation.
