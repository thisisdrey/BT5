# Q2468: Vault async redeem flow: partial withdraw / ratio drift / no stranded redeem

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with alternating redeem() and withdraw() claims against the same claimable redemption while the controller already has a prior claimable redeem or pending deposit position and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value and leading to Unintended or unfair fund distribution through excess asset withdrawals?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: alternating redeem() and withdraw() claims against the same claimable redemption
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value
- Expected Immunefi impact: Unintended or unfair fund distribution through excess asset withdrawals
- Fast validation: Forge test repeated redeem()/withdraw() calls against partially approved redemptions and assert reserve counters stay exact.
