# Q2470: Vault async redeem flow: partial withdraw / dust strand / global reserve sum

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with alternating redeem() and withdraw() claims against the same claimable redemption while the controller already has a prior claimable redeem or pending deposit position and strand non-zero approved assets or shares that can no longer be withdrawn or cancelled, breaking the rule that totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers and leading to Cross-shareholder dilution through broken async redemption accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: alternating redeem() and withdraw() claims against the same claimable redemption
- Exploit idea: strand non-zero approved assets or shares that can no longer be withdrawn or cancelled
- Invariant to test: totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers
- Expected Immunefi impact: Cross-shareholder dilution through broken async redemption accounting
- Fast validation: Model a tight-liquidity approval followed by partial claims and assert no controller can extract more than the burned shares justified.
