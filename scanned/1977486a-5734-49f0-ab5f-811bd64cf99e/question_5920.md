# Q5920: get-full-position via collateral-add: compose two individually correct mechanisms into an incorr

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls the position's existing collateral and debt composition reach `get-full-position` (mainnet/contracts/market/v0-4-market.clar:470) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it returns all collateral rows regardless of the enabled bitmap, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:470` -> `get-full-position`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the position's existing collateral and debt composition
- Exploit idea: `get-full-position` returns all collateral rows regardless of the enabled bitmap. Reach it through `collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `collateral-add` with the position's existing collateral and debt composition, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
