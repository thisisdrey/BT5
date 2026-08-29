# Q3720: get-liquidation-position via liquidate: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `min-collateral-expected` reach `get-liquidation-position` (mainnet/contracts/market/v0-4-market.clar:473) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it returns enabled collateral plus ALL debt, a different view from the one borrow validated against, the invariant that only the acting principal's own position is mutated breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:473` -> `get-liquidation-position`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `get-liquidation-position` returns enabled collateral plus ALL debt, a different view from the one borrow validated against. Reach it through `liquidate` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `min-collateral-expected` across its boundary values through `liquidate` in simnet and assert `get-liquidation-position` never returns a value that breaks the invariant.
