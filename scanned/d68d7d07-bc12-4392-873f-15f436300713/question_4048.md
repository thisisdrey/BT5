# Q4048: accrue-user-debts via liquidate-redeem: convert a rounding direction into a repeatable extraction

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the borrower targeted reach `accrue-user-debts` (mainnet/contracts/market/v0-4-market.clar:259) in a state where it convert a rounding direction into a repeatable extraction? Given that it folds accrual over the position's debt list only, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:259` -> `accrue-user-debts`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the borrower targeted
- Exploit idea: `accrue-user-debts` folds accrual over the position's debt list only. Reach it through `liquidate-redeem` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `liquidate-redeem` with the borrower targeted, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
