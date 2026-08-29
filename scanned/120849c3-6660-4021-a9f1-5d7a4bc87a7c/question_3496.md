# Q3496: resolve-or-create via repay: reach a state the guard immediately upstream of it never c

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls the `ft` trait principal reach `resolve-or-create` (mainnet/contracts/market/v0-market-vault.clar:143) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it allocates a user id through `increment` for whatever principal the market names, the invariant that only the acting principal's own position is mutated breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:143` -> `resolve-or-create`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `resolve-or-create` allocates a user id through `increment` for whatever principal the market names. Reach it through `repay` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `repay` with the `ft` trait principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
