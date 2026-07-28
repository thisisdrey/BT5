# Q2080: Vault async deposit flow: multi-request / counter divergence / no stranded claim

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with multiple pending deposit requests for the same controller before any claim while the manager approves only part of the pending assets before later approvals and make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value and leading to User funds stuck because approved deposits become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: multiple pending deposit requests for the same controller before any claim
- Exploit idea: make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value
- Expected Immunefi impact: User funds stuck because approved deposits become partially unclaimable
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
