# Q2353: Vault async redeem flow: operator churn / ratio drift / proportional reserve

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with operator approvals added or removed around request, approval, and claim boundaries while idleLiquidity is just above the approved reserve and later user activity could consume nearby balances and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim and leading to Unintended or unfair fund distribution through excess asset withdrawals?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: operator approvals added or removed around request, approval, and claim boundaries
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim
- Expected Immunefi impact: Unintended or unfair fund distribution through excess asset withdrawals
- Fast validation: Fuzz operator churn around redeem claims and ensure only the current authorized controller path can consume reserved assets.
