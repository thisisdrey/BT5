# Q4308: oracle-price-legal via liquidate: convert a rounding direction into a repeatable extraction

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `collateral-receiver` reach `oracle-price-legal` (mainnet/contracts/market/v0-4-market.clar:362) in a state where it convert a rounding direction into a repeatable extraction? Given that it accepts any price strictly greater than zero, with no upper bound and no sanity band, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:362` -> `oracle-price-legal`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `collateral-receiver`
- Exploit idea: `oracle-price-legal` accepts any price strictly greater than zero, with no upper bound and no sanity band. Reach it through `liquidate` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `collateral-receiver` across its boundary values through `liquidate` in simnet and assert `oracle-price-legal` never returns a value that breaks the invariant.
