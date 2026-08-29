# Q5280: unwrap-status via collateral-remove: make a health check read a different position than the one

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `price-feeds` buffers reach `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) in a state where it make a health check read a different position than the one that will exist? Given that it resolves `status` with `unwrap-panic`, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the `price-feeds` buffers across its boundary values through `collateral-remove` in simnet and assert `unwrap-status` never returns a value that breaks the invariant.
