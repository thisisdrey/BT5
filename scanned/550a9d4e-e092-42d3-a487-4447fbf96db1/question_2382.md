# Q2382: Vault async redeem flow: multi-redeem / reserve mismatch / global reserve sum

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while the manager approves redemptions while `lastNav` is fresh and `navStart` is idle and make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC, breaking the rule that totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers and leading to Accounting issue in the vault leading to underreserved claimable redemptions?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC
- Invariant to test: totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers
- Expected Immunefi impact: Accounting issue in the vault leading to underreserved claimable redemptions
- Fast validation: Fuzz operator churn around redeem claims and ensure only the current authorized controller path can consume reserved assets.
