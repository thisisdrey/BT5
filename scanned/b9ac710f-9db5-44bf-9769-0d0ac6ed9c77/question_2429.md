# Q2429: Vault async redeem flow: multi-redeem / reserve mismatch / proportional reserve

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while idleLiquidity is just above the approved reserve and later user activity could consume nearby balances and make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC, breaking the rule that claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim and leading to Unintended or unfair fund distribution through excess asset withdrawals?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC
- Invariant to test: claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim
- Expected Immunefi impact: Unintended or unfair fund distribution through excess asset withdrawals
- Fast validation: Fuzz operator churn around redeem claims and ensure only the current authorized controller path can consume reserved assets.
