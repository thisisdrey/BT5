# Q3567: filter-out-debt-asset via collateral-remove: make an aggregate and its per-item breakdown disagree

## Question
`filter-out-debt-asset` (mainnet/contracts/market/v0-4-market.clar:633) rebuilds the debt list without one asset, under `as-max-len? ... u64`. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing the set of assets held, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:633` -> `filter-out-debt-asset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `filter-out-debt-asset` rebuilds the debt list without one asset, under `as-max-len? ... u64`. Reach it through `collateral-remove` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `filter-out-debt-asset` touches, run `collateral-remove` with the set of assets held, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
