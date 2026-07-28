# Q2530: Vault async redeem flow: share batching / ratio drift / global reserve sum

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with share amounts chosen to maximize rounding boundaries across several approvals while the controller already has a prior claimable redeem or pending deposit position and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers and leading to Cross-shareholder dilution through broken async redemption accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: share amounts chosen to maximize rounding boundaries across several approvals
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers
- Expected Immunefi impact: Cross-shareholder dilution through broken async redemption accounting
- Fast validation: Model a tight-liquidity approval followed by partial claims and assert no controller can extract more than the burned shares justified.
