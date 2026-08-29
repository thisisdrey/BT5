# Q1380: find-asset via collateral-remove: make an aggregate and its per-item breakdown disagree

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls `amount` relative to the current collateral row (the removing-all branch) reach `find-asset` (mainnet/contracts/market/v0-4-market.clar:584) in a state where it make an aggregate and its per-item breakdown disagree? Given that it returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:584` -> `find-asset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `amount` relative to the current collateral row (the removing-all branch)
- Exploit idea: `find-asset` returns `none` when the id is absent, and several callers resolve that with `unwrap-panic`. Reach it through `collateral-remove` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` relative to the current collateral row (the removing-all branch) across its boundary values through `collateral-remove` in simnet and assert `find-asset` never returns a value that breaks the invariant.
