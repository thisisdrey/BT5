# Q1261: get-bitmap via liquidate: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), controlling `debt-amount`, drive `get-bitmap` (mainnet/contracts/registry/v0-assets.clar:145) — which returns the global enabled bitmap that every position read filters on — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that no position row exists that the position mask does not represent, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:145` -> `get-bitmap`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `debt-amount`
- Exploit idea: `get-bitmap` returns the global enabled bitmap that every position read filters on. Reach it through `liquidate` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate` with `debt-amount`, then read `get-bitmap` state before and after in the same block and assert the two sides of the invariant are equal.
