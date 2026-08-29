# Q3231: find-asset via liquidate: make an aggregate and its per-item breakdown disagree

## Question
`find-asset` (mainnet/contracts/market/v0-4-market.clar:584) returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing the `price-feeds` buffers and their ordering, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:584` -> `find-asset`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers and their ordering
- Exploit idea: `find-asset` returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Reach it through `liquidate` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `find-asset` touches, run `liquidate` with the `price-feeds` buffers and their ordering, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
