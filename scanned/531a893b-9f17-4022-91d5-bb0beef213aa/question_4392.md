# Q4392: get-liquidation-position via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls the position's existing collateral and debt composition reach `get-liquidation-position` (mainnet/contracts/market/v0-4-market.clar:473) in a state where it convert a rounding direction into a repeatable extraction? Given that it returns enabled collateral plus ALL debt, a different view from the one borrow validated against, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:473` -> `get-liquidation-position`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the position's existing collateral and debt composition
- Exploit idea: `get-liquidation-position` returns enabled collateral plus ALL debt, a different view from the one borrow validated against. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the position's existing collateral and debt composition across its boundary values through `collateral-add` in simnet and assert `get-liquidation-position` never returns a value that breaks the invariant.
