# Q1993: Vault async deposit flow: operator timing / price-lock break / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with an operator approval added and removed around request and claim boundaries while the manager approves while `lastNav` is fresh and non-zero and make the shares claimed exceed the approval-time price lock for the same controller, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Cross-shareholder dilution through broken async deposit accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: an operator approval added and removed around request and claim boundaries
- Exploit idea: make the shares claimed exceed the approval-time price lock for the same controller
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Cross-shareholder dilution through broken async deposit accounting
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
