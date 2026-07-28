# Q2050: Vault async deposit flow: multi-request / ratio drift / pending sum

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with multiple pending deposit requests for the same controller before any claim while the manager approves while `lastNav` is fresh and non-zero and make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims, breaking the rule that totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers and leading to Cross-shareholder dilution through broken async deposit accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: multiple pending deposit requests for the same controller before any claim
- Exploit idea: make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims
- Invariant to test: totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers
- Expected Immunefi impact: Cross-shareholder dilution through broken async deposit accounting
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
