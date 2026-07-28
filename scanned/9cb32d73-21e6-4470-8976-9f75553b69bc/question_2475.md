# Q2475: Vault async redeem flow: partial withdraw / free asset edge / burned-share fairness

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with alternating redeem() and withdraw() claims against the same claimable redemption while the controller already has a prior claimable redeem or pending deposit position and make withdraw() or redeem() round one side to zero while still transferring value on the other side, breaking the rule that shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed and leading to User funds stuck because approved redemptions become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: alternating redeem() and withdraw() claims against the same claimable redemption
- Exploit idea: make withdraw() or redeem() round one side to zero while still transferring value on the other side
- Invariant to test: shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed
- Expected Immunefi impact: User funds stuck because approved redemptions become partially unclaimable
- Fast validation: Exhaust a controller's claimable redemption in alternating share and asset units and assert no dust or reserve mismatch remains.
