# Q5953: get-asset-value via collateral-add: satisfy a bound with a value the bound was never designed 

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling the `ft` trait principal, drive `get-asset-value` (mainnet/contracts/market/v0-4-market.clar:679) — which resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction — to satisfy a bound with a value the bound was never designed to admit, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:679` -> `get-asset-value`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `get-asset-value` resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction. Reach it through `collateral-add` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-add` with the `ft` trait principal, then read `get-asset-value` state before and after in the same block and assert the two sides of the invariant are equal.
