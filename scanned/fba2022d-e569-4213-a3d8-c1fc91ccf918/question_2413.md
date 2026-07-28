# Q2413: Vault async redeem flow: multi-redeem / reserve mismatch / proportional reserve

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while the controller already has a prior claimable redeem or pending deposit position and make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC, breaking the rule that claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim and leading to Cross-shareholder dilution through broken async redemption accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC
- Invariant to test: claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim
- Expected Immunefi impact: Cross-shareholder dilution through broken async redemption accounting
- Fast validation: Model a tight-liquidity approval followed by partial claims and assert no controller can extract more than the burned shares justified.
