# Q1956: filter-u128 via collateral-add: make an aggregate and its per-item breakdown disagree

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls the `ft` trait principal reach `filter-u128` (mainnet/contracts/registry/v0-egroup.clar:97) in a state where it make an aggregate and its per-item breakdown disagree? Given that it filters a 128-entry bucket list, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:97` -> `filter-u128`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `filter-u128` filters a 128-entry bucket list. Reach it through `collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the `ft` trait principal across its boundary values through `collateral-add` in simnet and assert `filter-u128` never returns a value that breaks the invariant.
