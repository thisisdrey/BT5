# Q3865: get-liquidation-position via supply-collateral-add: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), controlling the `ft` trait principal deciding which vault is routed to, drive `get-liquidation-position` (mainnet/contracts/market/v0-4-market.clar:473) — which returns enabled collateral plus ALL debt, a different view from the one borrow validated against — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:473` -> `get-liquidation-position`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `get-liquidation-position` returns enabled collateral plus ALL debt, a different view from the one borrow validated against. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `supply-collateral-add` with the `ft` trait principal deciding which vault is routed to, then read `get-liquidation-position` state before and after in the same block and assert the two sides of the invariant are equal.
