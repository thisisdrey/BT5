# Q2451: Vault async redeem flow: partial withdraw / ratio drift / burned-share fairness

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with alternating redeem() and withdraw() claims against the same claimable redemption while the manager approves only part of the pending shares before later approvals and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed and leading to Accounting issue in the vault leading to underreserved claimable redemptions?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: alternating redeem() and withdraw() claims against the same claimable redemption
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed
- Expected Immunefi impact: Accounting issue in the vault leading to underreserved claimable redemptions
- Fast validation: Forge test repeated redeem()/withdraw() calls against partially approved redemptions and assert reserve counters stay exact.
