# Q2413: get-bitmap via collateral-add: convert a rounding direction into a repeatable extraction

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling the three `price-feeds` buffers and their order, drive `get-bitmap` (mainnet/contracts/registry/v0-assets.clar:145) — which returns the global enabled bitmap that every position read filters on — to convert a rounding direction into a repeatable extraction, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:145` -> `get-bitmap`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the three `price-feeds` buffers and their order
- Exploit idea: `get-bitmap` returns the global enabled bitmap that every position read filters on. Reach it through `collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-add` with the three `price-feeds` buffers and their order, then read `get-bitmap` state before and after in the same block and assert the two sides of the invariant are equal.
