# Q2521: Vault async redeem flow: share batching / free asset edge / proportional reserve

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with share amounts chosen to maximize rounding boundaries across several approvals while the manager approves only part of the pending shares before later approvals and make withdraw() or redeem() round one side to zero while still transferring value on the other side, breaking the rule that claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim and leading to Cross-shareholder dilution through broken async redemption accounting?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: share amounts chosen to maximize rounding boundaries across several approvals
- Exploit idea: make withdraw() or redeem() round one side to zero while still transferring value on the other side
- Invariant to test: claimableRedeemShares[controller] and claimableRedeemAssets[controller] should decay proportionally to each claim
- Expected Immunefi impact: Cross-shareholder dilution through broken async redemption accounting
- Fast validation: Exhaust a controller's claimable redemption in alternating share and asset units and assert no dust or reserve mismatch remains.
