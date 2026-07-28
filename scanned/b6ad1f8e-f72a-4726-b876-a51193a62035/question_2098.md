# Q2098: Vault async deposit flow: multi-request / ratio drift / pending sum

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with multiple pending deposit requests for the same controller before any claim while a holdings or ledger change zeroes `lastNavUpdate` between request and the next approval cycle and make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims, breaking the rule that totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers and leading to Accounting issue in the vault leading to underbacked claimable shares?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: multiple pending deposit requests for the same controller before any claim
- Exploit idea: make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims
- Invariant to test: totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claimable shares
- Fast validation: Forge test two requests plus multiple partial approvals, then claim in alternating asset and share units and assert counter conservation.
