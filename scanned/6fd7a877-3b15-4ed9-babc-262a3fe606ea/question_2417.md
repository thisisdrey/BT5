# Q2417: Vault async redeem flow: multi-redeem / ratio drift / proportional reserve

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while idleLiquidity is just above the approved reserve and later user activity could consume nearby balances and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim and leading to User funds stuck because approved redemptions become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim
- Expected Immunefi impact: User funds stuck because approved redemptions become partially unclaimable
- Fast validation: Model a tight-liquidity approval followed by partial claims and assert no controller can extract more than the burned shares justified.
