# Q2326: resolve-interpolation-points via borrow: compose two individually correct mechanisms into an incorr

## Question
Entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) while controlling the order of accrual versus price resolution inside the let, can an unprivileged attacker make `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) compose two individually correct mechanisms into an incorrect result? `resolve-interpolation-points` selects the bracketing curve points for a utilization, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the order of accrual versus price resolution inside the let
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `borrow` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `borrow` with the order of accrual versus price resolution inside the let, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
