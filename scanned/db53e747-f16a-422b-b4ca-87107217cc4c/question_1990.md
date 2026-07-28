# Q1990: Vault async deposit flow: operator timing / dust strand / pending sum

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with an operator approval added and removed around request and claim boundaries while the manager approves while `lastNav` is fresh and non-zero and strand non-zero approved assets or shares that can no longer be claimed or cancelled, breaking the rule that totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers and leading to Cross-shareholder dilution through broken async deposit accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: an operator approval added and removed around request and claim boundaries
- Exploit idea: strand non-zero approved assets or shares that can no longer be claimed or cancelled
- Invariant to test: totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers
- Expected Immunefi impact: Cross-shareholder dilution through broken async deposit accounting
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
