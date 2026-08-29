# Q1812: oracle-price-legal via supply-collateral-add: make an aggregate and its per-item breakdown disagree

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the `ft` trait principal deciding which vault is routed to reach `oracle-price-legal` (mainnet/contracts/market/v0-4-market.clar:362) in a state where it make an aggregate and its per-item breakdown disagree? Given that it accepts any price strictly greater than zero, with no upper bound and no sanity band, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:362` -> `oracle-price-legal`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `oracle-price-legal` accepts any price strictly greater than zero, with no upper bound and no sanity band. Reach it through `supply-collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the `ft` trait principal deciding which vault is routed to across its boundary values through `supply-collateral-add` in simnet and assert `oracle-price-legal` never returns a value that breaks the invariant.
