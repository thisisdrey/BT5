# Q1609: filter-out-debt-asset via collateral-remove: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), controlling `receiver`, including a contract principal, drive `filter-out-debt-asset` (mainnet/contracts/market/v0-4-market.clar:633) — which rebuilds the debt list without one asset, under `as-max-len? ... u64` — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:633` -> `filter-out-debt-asset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `filter-out-debt-asset` rebuilds the debt list without one asset, under `as-max-len? ... u64`. Reach it through `collateral-remove` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-remove` with `receiver`, including a contract principal, then read `filter-out-debt-asset` state before and after in the same block and assert the two sides of the invariant are equal.
