# Q5124: resolve via collateral-remove: make a health check read a different position than the one

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `price-feeds` buffers reach `resolve` (mainnet/contracts/registry/v0-egroup.clar:360) in a state where it make a health check read a different position than the one that will exist? Given that it selects the efficiency group for a position mask, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:360` -> `resolve`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `resolve` selects the efficiency group for a position mask. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the `price-feeds` buffers across its boundary values through `collateral-remove` in simnet and assert `resolve` never returns a value that breaks the invariant.
