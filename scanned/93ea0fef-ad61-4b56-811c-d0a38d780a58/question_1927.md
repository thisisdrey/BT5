# Q1927: Vault async deposit flow: split parties / dust strand / approved price

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with owner, controller, and receiver split across attacker-controlled addresses while the manager approves while `lastNav` is fresh and non-zero and strand non-zero approved assets or shares that can no longer be claimed or cancelled, breaking the rule that approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch and leading to Cross-shareholder dilution through broken async deposit accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: owner, controller, and receiver split across attacker-controlled addresses
- Exploit idea: strand non-zero approved assets or shares that can no longer be claimed or cancelled
- Invariant to test: approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch
- Expected Immunefi impact: Cross-shareholder dilution through broken async deposit accounting
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
