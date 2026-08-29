# Q4348: filter-u128 via collateral-remove: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls `receiver`, including a contract principal reach `filter-u128` (mainnet/contracts/registry/v0-egroup.clar:97) in a state where it convert a rounding direction into a repeatable extraction? Given that it filters a 128-entry bucket list, the invariant that no position row exists that the position mask does not represent breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:97` -> `filter-u128`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `filter-u128` filters a 128-entry bucket list. Reach it through `collateral-remove` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `collateral-remove` with `receiver`, including a contract principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
