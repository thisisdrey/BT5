# Q2391: Vault async redeem flow: multi-redeem / dust strand / burned-share fairness

## Question
Can a whitelisted shareholder or operator using only normal redeem and withdraw entrypoints enter through `PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest` with multiple pending redeem requests for the same controller before any claim while the manager approves only part of the pending shares before later approvals and strand non-zero approved assets or shares that can no longer be withdrawn or cancelled, breaking the rule that shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed and leading to Accounting issue in the vault leading to underreserved claimable redemptions?

## Target
- File/function: contracts/PortfolioVault.sol / requestRedeem -> approveRedemption -> redeem/withdraw -> cancelRedeemRequest
- Entrypoint: PortfolioVault.requestRedeem/redeem/withdraw/cancelRedeemRequest
- Attacker controls: multiple pending redeem requests for the same controller before any claim
- Exploit idea: strand non-zero approved assets or shares that can no longer be withdrawn or cancelled
- Invariant to test: shares burned at approval should never let a controller withdraw more assets than the approval-time NAV allowed
- Expected Immunefi impact: Accounting issue in the vault leading to underreserved claimable redemptions
- Fast validation: Forge test repeated redeem()/withdraw() calls against partially approved redemptions and assert reserve counters stay exact.
