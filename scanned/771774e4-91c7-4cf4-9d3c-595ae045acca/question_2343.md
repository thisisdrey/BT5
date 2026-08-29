# Q2343: unwrap-status via collateral-remove: satisfy a bound with a value the bound was never designed 

## Question
`unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) resolves `status` with `unwrap-panic`. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing the `price-feeds` buffers, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `collateral-remove` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `unwrap-status` touches, run `collateral-remove` with the `price-feeds` buffers, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
