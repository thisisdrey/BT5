# Q1364: unwrap-status via collateral-remove: make an aggregate and its per-item breakdown disagree

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `ft` trait principal reach `unwrap-status` (mainnet/contracts/registry/v0-assets.clar:111) in a state where it make an aggregate and its per-item breakdown disagree? Given that it resolves `status` with `unwrap-panic`, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:111` -> `unwrap-status`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `unwrap-status` resolves `status` with `unwrap-panic`. Reach it through `collateral-remove` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-remove` twice with the `ft` trait principal varied, and assert that the value `unwrap-status` returns is identical in both runs; a divergence confirms the finding.
