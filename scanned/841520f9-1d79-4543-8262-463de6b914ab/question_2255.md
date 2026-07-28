# Q2255: Vault async redeem flow: split owner-controller / reserve mismatch / burned-share fairness

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with owner, controller, and receiver split across attacker-controlled addresses while the manager approves redemptions while `lastNav` is fresh and `navStart` is idle and make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC, breaking the rule that shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed and leading to User funds stuck because approved redemptions become partially unclaimable?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: owner, controller, and receiver split across attacker-controlled addresses
- Exploit idea: make totalClaimableRedeemAssets or the per-controller counters diverge from the actual reserved USDC
- Invariant to test: shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed
- Expected Immunefi impact: User funds stuck because approved redemptions become partially unclaimable
- Fast validation: Exhaust a controller's claimable redemption in alternating share and asset units and assert no dust or reserve mismatch remains.
