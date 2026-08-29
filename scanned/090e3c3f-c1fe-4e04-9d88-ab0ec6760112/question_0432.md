# Q0432: filter-u128 via collateral-remove: satisfy a bound with a value the bound was never designed 

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the set of assets held reach `filter-u128` (mainnet/contracts/registry/v0-egroup.clar:97) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it filters a 128-entry bucket list, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:97` -> `filter-u128`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `filter-u128` filters a 128-entry bucket list. Reach it through `collateral-remove` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the set of assets held across its boundary values through `collateral-remove` in simnet and assert `filter-u128` never returns a value that breaks the invariant.
