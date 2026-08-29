# Q0292: get-account-scaled-debt via repay: satisfy a bound with a value the bound was never designed 

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls whether the repaid asset is in the accrued debt list reach `get-account-scaled-debt` (mainnet/contracts/market/v0-market-vault.clar:307) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it reads one scaled debt row, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:307` -> `get-account-scaled-debt`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `get-account-scaled-debt` reads one scaled debt row. Reach it through `repay` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `repay` with whether the repaid asset is in the accrued debt list, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
