# Q2005: Vault async deposit flow: operator timing / dust strand / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with an operator approval added and removed around request and claim boundaries while the manager approves only part of the pending assets before later approvals and strand non-zero approved assets or shares that can no longer be claimed or cancelled, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Cross-shareholder dilution through broken async deposit accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: an operator approval added and removed around request and claim boundaries
- Exploit idea: strand non-zero approved assets or shares that can no longer be claimed or cancelled
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Cross-shareholder dilution through broken async deposit accounting
- Fast validation: Inject an approval after a NAV invalidation cycle and assert the final claim never exceeds the exact approval-time price lock.
