# Q5428: total-assets via collateral-remove-redeem: make a health check read a different position than the one

## Question
Does `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211) let an unprivileged attacker who controls `receiver` for the underlying leg reach `total-assets` (mainnet/contracts/vault/v0-vault-stx.clar:334) in a state where it make a health check read a different position than the one that will exist? Given that it adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:334` -> `total-assets`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `receiver` for the underlying leg
- Exploit idea: `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs. Reach it through `collateral-remove-redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `collateral-remove-redeem` with `receiver` for the underlying leg, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
