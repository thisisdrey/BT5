# Q3481: iter-find-superset via collateral-remove: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), controlling whether the position has any enabled debt row (the has-debt branch), drive `iter-find-superset` (mainnet/contracts/registry/v0-egroup.clar:267) — which short-circuits on the first superset match — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:267` -> `iter-find-superset`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: whether the position has any enabled debt row (the has-debt branch)
- Exploit idea: `iter-find-superset` short-circuits on the first superset match. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-remove` with whether the position has any enabled debt row (the has-debt branch), then read `iter-find-superset` state before and after in the same block and assert the two sides of the invariant are equal.
