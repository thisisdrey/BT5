# Q2412: Vault async redeem flow: multi-redeem / free asset edge / no stranded redeem

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while the controller already has a prior claimable redeem or pending deposit position and make withdraw() or redeem() round one side to zero while still transferring value on the other side, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value and leading to User funds stuck because approved redemptions become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: make withdraw() or redeem() round one side to zero while still transferring value on the other side
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value
- Expected Immunefi impact: User funds stuck because approved redemptions become partially unclaimable
- Fast validation: Exhaust a controller's claimable redemption in alternating share and asset units and assert no dust or reserve mismatch remains.
