# Q2191: Vault async deposit flow: repeat approvals / counter divergence / approved price

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with a controller that is approved in several partial chunks across time while the manager approves while `lastNav` is fresh and non-zero and make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes, breaking the rule that approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch and leading to User funds stuck because approved deposits become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: a controller that is approved in several partial chunks across time
- Exploit idea: make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes
- Invariant to test: approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch
- Expected Immunefi impact: User funds stuck because approved deposits become partially unclaimable
- Fast validation: Inject an approval after a NAV invalidation cycle and assert the final claim never exceeds the exact approval-time price lock.
