# Q2533: Vault async redeem flow: share batching / dust strand / proportional reserve

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with share amounts chosen to maximize rounding boundaries across several approvals while the controller already has a prior claimable redeem or pending deposit position and strand non-zero approved assets or shares that can no longer be withdrawn or cancelled, breaking the rule that claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim and leading to Cross-shareholder dilution through broken async redemption accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: share amounts chosen to maximize rounding boundaries across several approvals
- Exploit idea: strand non-zero approved assets or shares that can no longer be withdrawn or cancelled
- Invariant to test: claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim
- Expected Immunefi impact: Cross-shareholder dilution through broken async redemption accounting
- Fast validation: Model a tight-liquidity approval followed by partial claims and assert no controller can extract more than the burned shares justified.
