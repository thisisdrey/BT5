# Q1564: accrue-user-collateral via repay: make an aggregate and its per-item breakdown disagree

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `amount`, including far above the real debt (the capping path) reach `accrue-user-collateral` (mainnet/contracts/market/v0-4-market.clar:270) in a state where it make an aggregate and its per-item breakdown disagree? Given that it accrues only rows that `is-ztoken` recognises, skipping everything else, the invariant that conversions never round in the user's favour in either direction breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:270` -> `accrue-user-collateral`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `accrue-user-collateral` accrues only rows that `is-ztoken` recognises, skipping everything else. Reach it through `repay` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `repay` with `amount`, including far above the real debt (the capping path), and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
