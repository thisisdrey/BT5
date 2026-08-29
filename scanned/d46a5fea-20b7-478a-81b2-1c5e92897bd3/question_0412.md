# Q0412: is-healthy-with-mask via collateral-remove: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls whether the position has any enabled debt row (the has-debt branch) reach `is-healthy-with-mask` (mainnet/contracts/market/v0-4-market.clar:663) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it resolves an egroup for a caller-influenced mask and applies its LTV-BORROW, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:663` -> `is-healthy-with-mask`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: whether the position has any enabled debt row (the has-debt branch)
- Exploit idea: `is-healthy-with-mask` resolves an egroup for a caller-influenced mask and applies its LTV-BORROW. Reach it through `collateral-remove` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `collateral-remove` with whether the position has any enabled debt row (the has-debt branch), and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
