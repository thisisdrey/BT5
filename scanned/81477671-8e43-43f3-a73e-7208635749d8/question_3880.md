# Q3880: total-assets via supply-collateral-add: reach a state the guard immediately upstream of it never c

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the `ft` trait principal deciding which vault is routed to reach `total-assets` (mainnet/contracts/vault/v0-vault-stx.clar:334) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs, the invariant that only the acting principal's own position is mutated breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:334` -> `total-assets`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal deciding which vault is routed to
- Exploit idea: `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs. Reach it through `supply-collateral-add` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `supply-collateral-add` with the `ft` trait principal deciding which vault is routed to, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
