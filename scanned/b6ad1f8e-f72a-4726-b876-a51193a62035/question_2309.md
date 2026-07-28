# Q2309: Vault async redeem flow: operator churn / dust strand / proportional reserve

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with operator approvals added or removed around request, approval, and claim boundaries while the manager approves redemptions while `lastNav` is fresh and `navStart` is idle and strand non-zero approved assets or shares that can no longer be withdrawn or cancelled, breaking the rule that claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim and leading to Accounting issue in the vault leading to underreserved claimable redemptions?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: operator approvals added or removed around request, approval, and claim boundaries
- Exploit idea: strand non-zero approved assets or shares that can no longer be withdrawn or cancelled
- Invariant to test: claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim
- Expected Immunefi impact: Accounting issue in the vault leading to underreserved claimable redemptions
- Fast validation: Fuzz operator churn around redeem claims and ensure only the current authorized controller path can consume reserved assets.
