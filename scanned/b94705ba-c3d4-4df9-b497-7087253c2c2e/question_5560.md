# Q5560: vault-accrue via repay: make a health check read a different position than the one

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `amount`, including far above the real debt (the capping path) reach `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) in a state where it make a health check read a different position than the one that will exist? Given that it dispatches accrual to one of six vaults by asset id, the invariant that conversions never round in the user's favour in either direction breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `repay` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `repay` with `amount`, including far above the real debt (the capping path), and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
