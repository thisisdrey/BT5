# Q2067: Vault async deposit flow: multi-request / ratio drift / approved price

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with multiple pending deposit requests for the same controller before any claim while the manager approves only part of the pending assets before later approvals and make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims, breaking the rule that approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch and leading to User funds stuck because approved deposits become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: multiple pending deposit requests for the same controller before any claim
- Exploit idea: make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims
- Invariant to test: approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch
- Expected Immunefi impact: User funds stuck because approved deposits become partially unclaimable
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
