# Q1468: calc-liq-factor-bound via liquidate: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls which collateral and debt asset pair is targeted reach `calc-liq-factor-bound` (mainnet/contracts/market/v0-4-market.clar:718) in a state where it make an aggregate and its per-item breakdown disagree? Given that it scales the penalty between a min and a max, capped at the max, the invariant that conversions never round in the user's favour in either direction breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:718` -> `calc-liq-factor-bound`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `calc-liq-factor-bound` scales the penalty between a min and a max, capped at the max. Reach it through `liquidate` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `liquidate` with which collateral and debt asset pair is targeted, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
