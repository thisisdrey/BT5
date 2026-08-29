# Q1132: resolve-dia via collateral-remove: make an aggregate and its per-item breakdown disagree

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `price-feeds` buffers reach `resolve-dia` (mainnet/contracts/market/v0-4-market.clar:326) in a state where it make an aggregate and its per-item breakdown disagree? Given that it derives a (string-ascii 32) key from a (buff 32) ident, the invariant that conversions never round in the user's favour in either direction breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:326` -> `resolve-dia`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `resolve-dia` derives a (string-ascii 32) key from a (buff 32) ident. Reach it through `collateral-remove` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `collateral-remove` with the `price-feeds` buffers, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
