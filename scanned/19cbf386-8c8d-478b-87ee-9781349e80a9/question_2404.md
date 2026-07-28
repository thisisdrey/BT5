# Q2404: Vault async redeem flow: multi-redeem / ratio drift / no stranded redeem

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while the controller already has a prior claimable redeem or pending deposit position and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value and leading to Cross-shareholder dilution through broken async redemption accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value
- Expected Immunefi impact: Cross-shareholder dilution through broken async redemption accounting
- Fast validation: Model a tight-liquidity approval followed by partial claims and assert no controller can extract more than the burned shares justified.
