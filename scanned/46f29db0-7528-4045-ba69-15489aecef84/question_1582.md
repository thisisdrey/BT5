# Q1582: status via collateral-add: make a health check read a different position than the one

## Question
Entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) while controlling whether this asset is already collateral (the is-new-collateral branch), can an unprivileged attacker make `status` (mainnet/contracts/registry/v0-assets.clar:115) make a health check read a different position than the one that will exist? `status` derives `collateral` and `debt` flags from bit tests against whatever mask it was handed, so the invariant that only the acting principal's own position is mutated would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:115` -> `status`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: whether this asset is already collateral (the is-new-collateral branch)
- Exploit idea: `status` derives `collateral` and `debt` flags from bit tests against whatever mask it was handed. Reach it through `collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `collateral-add` with whether this asset is already collateral (the is-new-collateral branch), and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
