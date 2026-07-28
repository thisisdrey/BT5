# Q2388: Vault async redeem flow: multi-redeem / ratio drift / no stranded redeem

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while the manager approves only part of the pending shares before later approvals and make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims, breaking the rule that no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value and leading to Accounting issue in the vault leading to underreserved claimable redemptions?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: make claimableRedeemShares and claimableRedeemAssets decay out of proportion during repeated claims
- Invariant to test: no sequence of partial approvals and partial claims should leave a live controller with permanently unclaimable redemption value
- Expected Immunefi impact: Accounting issue in the vault leading to underreserved claimable redemptions
- Fast validation: Forge test repeated redeem()/withdraw() calls against partially approved redemptions and assert reserve counters stay exact.
