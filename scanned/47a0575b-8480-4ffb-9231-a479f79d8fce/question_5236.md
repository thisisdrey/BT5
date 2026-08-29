# Q5236: convert-to-scaled-debt via collateral-remove: make a health check read a different position than the one

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `ft` trait principal reach `convert-to-scaled-debt` (mainnet/contracts/market/v0-4-market.clar:648) in a state where it make a health check read a different position than the one that will exist? Given that it scales a token amount by the cached borrow index, rounding up on the borrow path, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:648` -> `convert-to-scaled-debt`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `convert-to-scaled-debt` scales a token amount by the cached borrow index, rounding up on the borrow path. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `collateral-remove` with the `ft` trait principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
