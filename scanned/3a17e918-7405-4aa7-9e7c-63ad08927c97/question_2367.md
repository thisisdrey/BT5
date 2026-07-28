# Q2367: Vault async redeem flow: operator churn / reserve mismatch / burned-share fairness

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with operator approvals added or removed around request, approval, and claim boundaries while idleLiquidity is just above the approved reserve and later user activity could consume nearby balances and make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC, breaking the rule that shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed and leading to User funds stuck because approved redemptions become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: operator approvals added or removed around request, approval, and claim boundaries
- Exploit idea: make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC
- Invariant to test: shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed
- Expected Immunefi impact: User funds stuck because approved redemptions become partially unclaimable
- Fast validation: Model a tight-liquidity approval followed by partial claims and assert no controller can extract more than the burned shares justified.
