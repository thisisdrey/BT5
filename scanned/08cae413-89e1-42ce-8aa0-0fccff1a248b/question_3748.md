# Q3748: increment via collateral-add: reach a state the guard immediately upstream of it never c

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls the `ft` trait principal reach `increment` (mainnet/contracts/market/v0-market-vault.clar:137) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it advances the user-id nonce, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:137` -> `increment`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `increment` advances the user-id nonce. Reach it through `collateral-add` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `collateral-add` with the `ft` trait principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
