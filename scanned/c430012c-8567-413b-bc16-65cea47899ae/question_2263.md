# Q2263: Vault async redeem flow: split owner-controller / dust strand / burned-share fairness

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with owner, controller, and receiver split across attacker-controlled addresses while the manager approves only part of the pending shares before later approvals and strand non-zero approved assets or shares that can no longer be withdrawn or cancelled, breaking the rule that shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed and leading to Unintended or unfair fund distribution through excess asset withdrawals?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: owner, controller, and receiver split across attacker-controlled addresses
- Exploit idea: strand non-zero approved assets or shares that can no longer be withdrawn or cancelled
- Invariant to test: shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed
- Expected Immunefi impact: Unintended or unfair fund distribution through excess asset withdrawals
- Fast validation: Fuzz operator churn around redeem claims and ensure only the current authorized controller path can consume reserved assets.
