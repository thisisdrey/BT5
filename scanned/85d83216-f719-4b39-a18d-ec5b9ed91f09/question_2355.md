# Q2355: Vault async redeem flow: operator churn / ratio drift / burned-share fairness

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with operator approvals added or removed around request, approval, and claim boundaries while idleLiquidity is just above the approved reserve and later user activity could consume nearby balances and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed and leading to Accounting issue in the vault leading to underreserved claimable redemptions?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: operator approvals added or removed around request, approval, and claim boundaries
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed
- Expected Immunefi impact: Accounting issue in the vault leading to underreserved claimable redemptions
- Fast validation: Forge test repeated redeem()/withdraw() calls against partially approved redemptions and assert reserve counters stay exact.
