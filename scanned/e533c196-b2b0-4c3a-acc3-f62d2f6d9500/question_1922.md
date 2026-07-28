# Q1922: Vault async deposit flow: split parties / ratio drift / pending sum

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with owner, controller, and receiver split across attacker-controlled addresses while the manager approves while `lastNav` is fresh and non-zero and make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims, breaking the rule that totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers and leading to User funds stuck because approved deposits become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: owner, controller, and receiver split across attacker-controlled addresses
- Exploit idea: make claimableDepositAssets and claimableDepositShares decay out of proportion during repeated claims
- Invariant to test: totalPendingDepositAssets should always equal the sum of pendingDepositAssets across controllers
- Expected Immunefi impact: User funds stuck because approved deposits become partially unclaimable
- Fast validation: Inject an approval after a NAV invalidation cycle and assert the final claim never exceeds the exact approval-time price lock.
