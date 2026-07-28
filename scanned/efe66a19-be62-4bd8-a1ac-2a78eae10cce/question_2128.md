# Q2128: Vault async deposit flow: partial claim / counter divergence / no stranded claim

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with alternating deposit() and mint() claims against the same claimable position while the manager approves while `lastNav` is fresh and non-zero and make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value and leading to User funds stuck because approved deposits become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: alternating deposit() and mint() claims against the same claimable position
- Exploit idea: make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable approved value
- Expected Immunefi impact: User funds stuck because approved deposits become partially unclaimable
- Fast validation: Inject an approval after a NAV invalidation cycle and assert the final claim never exceeds the exact approval-time price lock.
