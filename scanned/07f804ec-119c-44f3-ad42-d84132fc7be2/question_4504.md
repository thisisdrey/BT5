# Q4504: insert via supply-collateral-add: convert a rounding direction into a repeatable extraction

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the `ft` trait principal deciding which vault is routed to reach `insert` (mainnet/contracts/market/v0-market-vault.clar:159) in a state where it convert a rounding direction into a repeatable extraction? Given that it rewrites the whole registry entry for a user id, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:159` -> `insert`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `insert` rewrites the whole registry entry for a user id. Reach it through `supply-collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `supply-collateral-add` with the `ft` trait principal deciding which vault is routed to, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
