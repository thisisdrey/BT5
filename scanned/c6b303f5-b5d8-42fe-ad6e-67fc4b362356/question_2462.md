# Q2462: Vault async redeem flow: partial withdraw / reserve mismatch / global reserve sum

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with alternating redeem() and withdraw() claims against the same claimable redemption while the manager approves only part of the pending shares before later approvals and make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC, breaking the rule that totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers and leading to Unintended or unfair fund distribution through excess asset withdrawals?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: alternating redeem() and withdraw() claims against the same claimable redemption
- Exploit idea: make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC
- Invariant to test: totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers
- Expected Immunefi impact: Unintended or unfair fund distribution through excess asset withdrawals
- Fast validation: Fuzz operator churn around redeem claims and ensure only the current authorized controller path can consume reserved assets.
