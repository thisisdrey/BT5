# Q1967: Vault async deposit flow: split parties / counter divergence / approved price

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with owner, controller, and receiver split across attacker-controlled addresses while the controller already has a prior claimable deposit or pending redeem position and make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes, breaking the rule that approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch and leading to Cross-shareholder dilution through broken async deposit accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: owner, controller, and receiver split across attacker-controlled addresses
- Exploit idea: make totalPendingDepositAssets or the per-controller claimable counters diverge from what the vault actually owes
- Invariant to test: approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch
- Expected Immunefi impact: Cross-shareholder dilution through broken async deposit accounting
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
