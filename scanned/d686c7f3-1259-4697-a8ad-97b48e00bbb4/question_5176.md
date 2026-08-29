# Q5176: resolve-ststx via collateral-remove: make a health check read a different position than the one

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `ft` trait principal reach `resolve-ststx` (mainnet/contracts/market/v0-4-market.clar:339) in a state where it make a health check read a different position than the one that will exist? Given that it calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down`, the invariant that conversions never round in the user's favour in either direction breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:339` -> `resolve-ststx`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `resolve-ststx` calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down`. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `collateral-remove` with the `ft` trait principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
