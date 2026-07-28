# Q1947: Vault async deposit flow: split parties / price-lock break / approved price

## Question
Can a whitelisted shareholder controlling owner, controller, receiver, and optionally an operator enter through `PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest` with owner, controller, and receiver split across attacker-controlled addresses while the manager approves only part of the pending assets before later approvals and make the shares claimed exceed the approval-time price lock for the same controller, breaking the rule that approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch and leading to User funds stuck because approved deposits become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestDeposit -> approveDeposit -> deposit/mint -> cancelDepositRequest
- Entrypoint: PortfolioVault.requestDeposit/deposit/mint/cancelDepositRequest
- Attacker controls: owner, controller, and receiver split across attacker-controlled addresses
- Exploit idea: make the shares claimed exceed the approval-time price lock for the same controller
- Invariant to test: approved deposit shares should never exceed assets * totalSupply / approvalNAV for that exact approval epoch
- Expected Immunefi impact: User funds stuck because approved deposits become partially unclaimable
- Fast validation: Simulate partial approvals across time and assert claimable assets and shares can always be fully exhausted without dust-locking value.
