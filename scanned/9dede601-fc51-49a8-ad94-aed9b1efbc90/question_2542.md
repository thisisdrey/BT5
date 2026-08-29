# Q2542: accrue-user-debts via redeem: compose two individually correct mechanisms into an incorr

## Question
Entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) while controlling the vault's available liquidity relative to the redemption, can an unprivileged attacker make `accrue-user-debts` (mainnet/contracts/market/v0-4-market.clar:259) compose two individually correct mechanisms into an incorrect result? `accrue-user-debts` folds accrual over the position's debt list only, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:259` -> `accrue-user-debts`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: the vault's available liquidity relative to the redemption
- Exploit idea: `accrue-user-debts` folds accrual over the position's debt list only. Reach it through `redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `redeem` with the vault's available liquidity relative to the redemption, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
