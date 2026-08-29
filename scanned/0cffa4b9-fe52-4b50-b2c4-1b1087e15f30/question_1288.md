# Q1288: filter-out-debt-asset via liquidate-redeem: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the seized zToken amount that is immediately redeemed reach `filter-out-debt-asset` (mainnet/contracts/market/v0-4-market.clar:633) in a state where it make an aggregate and its per-item breakdown disagree? Given that it rebuilds the debt list without one asset, under `as-max-len? ... u64`, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:633` -> `filter-out-debt-asset`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `filter-out-debt-asset` rebuilds the debt list without one asset, under `as-max-len? ... u64`. Reach it through `liquidate-redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `liquidate-redeem` with the seized zToken amount that is immediately redeemed, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
