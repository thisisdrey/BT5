# Q1532: interpolate-rate via collateral-add: make an aggregate and its per-item breakdown disagree

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls the three `price-feeds` buffers and their order reach `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) in a state where it make an aggregate and its per-item breakdown disagree? Given that it interpolates between packed u16 curve points, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the three `price-feeds` buffers and their order
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with the three `price-feeds` buffers and their order varied, and assert that the value `interpolate-rate` returns is identical in both runs; a divergence confirms the finding.
