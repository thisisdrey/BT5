# Q3895: socialize-debt-asset via liquidate: make an aggregate and its per-item breakdown disagree

## Question
`socialize-debt-asset` (mainnet/contracts/market/v0-4-market.clar:879) calls the vault write-down, then overwrites `index-cache` for the current timestamp mid-fold, and carries a `success` flag that short-circuits. Can an unprivileged caller of `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), by choosing `collateral-receiver`, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that no position row exists that the position mask does not represent and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:879` -> `socialize-debt-asset`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `collateral-receiver`
- Exploit idea: `socialize-debt-asset` calls the vault write-down, then overwrites `index-cache` for the current timestamp mid-fold, and carries a `success` flag that short-circuits. Reach it through `liquidate` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate` with `collateral-receiver`, then read `socialize-debt-asset` state before and after in the same block and assert the two sides of the invariant are equal.
