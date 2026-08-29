# Q1348: accrue-user-collateral via liquidate-redeem: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the seized zToken amount that is immediately redeemed reach `accrue-user-collateral` (mainnet/contracts/market/v0-4-market.clar:270) in a state where it make an aggregate and its per-item breakdown disagree? Given that it accrues only rows that `is-ztoken` recognises, skipping everything else, the invariant that conversions never round in the user's favour in either direction breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:270` -> `accrue-user-collateral`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `accrue-user-collateral` accrues only rows that `is-ztoken` recognises, skipping everything else. Reach it through `liquidate-redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `liquidate-redeem` with the seized zToken amount that is immediately redeemed, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
