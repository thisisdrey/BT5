# Q2041: Vault async deposit flow: operator timing / price-lock break / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with an operator approval added and removed around request and claim boundaries while a holdings or ledger change zeroes `lastNavUpdate` between request and the next approval cycle and make the shares claimed exceed the approval-time price lock for the same controller, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Accounting issue in the vault leading to underbacked claimable shares?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: an operator approval added and removed around request and claim boundaries
- Exploit idea: make the shares claimed exceed the approval-time price lock for the same controller
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Accounting issue in the vault leading to underbacked claimable shares
- Fast validation: Forge test two requests plus multiple partial approvals, then claim in alternating asset and share units and assert counter conservation.
