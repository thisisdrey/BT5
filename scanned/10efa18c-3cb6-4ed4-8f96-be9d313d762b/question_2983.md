# Q2983: get-full-position via collateral-add: make an aggregate and its per-item breakdown disagree

## Question
`get-full-position` (mainnet/contracts/market/v0-4-market.clar:470) returns all collateral rows regardless of the enabled bitmap. Can an unprivileged caller of `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), by choosing the position's existing collateral and debt composition, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:470` -> `get-full-position`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the position's existing collateral and debt composition
- Exploit idea: `get-full-position` returns all collateral rows regardless of the enabled bitmap. Reach it through `collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-add` with the position's existing collateral and debt composition, then read `get-full-position` state before and after in the same block and assert the two sides of the invariant are equal.
