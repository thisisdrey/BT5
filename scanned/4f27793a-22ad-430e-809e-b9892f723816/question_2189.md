# Q2189: Vault async deposit flow: repeat approvals / counter divergence / proportional decay

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with a controller that is approved in several partial chunks across time while the manager approves while `lastNav` is fresh and non-zero and make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes, breaking the rule that claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim and leading to Cross-shareholder dilution through broken async deposit accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: a controller that is approved in several partial chunks across time
- Exploit idea: make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes
- Invariant to test: claimableDepositAssets[controller] and claimableDepositShares[controller] should decay proportionally to the actual claim
- Expected Immunefi impact: Cross-shareholder dilution through broken async deposit accounting
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
