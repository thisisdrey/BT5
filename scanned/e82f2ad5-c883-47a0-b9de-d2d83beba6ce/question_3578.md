# Q3578: mask-to-list-collateral via collateral-remove-redeem: turn an accounting residue into a permanently unclosable p

## Question
Entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) while controlling `amount` used for BOTH the collateral removal and the share redemption, can an unprivileged attacker make `mask-to-list-collateral` (mainnet/contracts/market/v0-4-market.clar:449) turn an accounting residue into a permanently unclosable position? `mask-to-list-collateral` expands a mask to a list of ids over ITER-UINT-64, so the invariant that a value cached within a block still describes the state it was derived from would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:449` -> `mask-to-list-collateral`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `amount` used for BOTH the collateral removal and the share redemption
- Exploit idea: `mask-to-list-collateral` expands a mask to a list of ids over ITER-UINT-64. Reach it through `collateral-remove-redeem` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-remove-redeem` twice with `amount` used for BOTH the collateral removal and the share redemption varied, and assert that the value `mask-to-list-collateral` returns is identical in both runs; a divergence confirms the finding.
