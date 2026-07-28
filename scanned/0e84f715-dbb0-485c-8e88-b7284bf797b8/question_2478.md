# Q2478: Vault async redeem flow: partial withdraw / reserve mismatch / global reserve sum

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with alternating redeem() and withdraw() claims against the same claimable redemption while the controller already has a prior claimable redeem or pending deposit position and make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC, breaking the rule that totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers and leading to User funds stuck because approved redemptions become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: alternating redeem() and withdraw() claims against the same claimable redemption
- Exploit idea: make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC
- Invariant to test: totalClaimableRedeemAssets should always equal the sum of claimableRedeemAssets across controllers
- Expected Immunefi impact: User funds stuck because approved redemptions become partially unclaimable
- Fast validation: Exhaust a controller's claimable redemption in alternating share and asset units and assert no dust or reserve mismatch remains.
