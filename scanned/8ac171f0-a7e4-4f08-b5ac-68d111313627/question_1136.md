# Q1136: uint-to-list-u64 via liquidate: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `min-collateral-expected` reach `uint-to-list-u64` (mainnet/contracts/registry/v0-assets.clar:80) in a state where it make an aggregate and its per-item breakdown disagree? Given that it expands a bitmap into a 64-element list, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:80` -> `uint-to-list-u64`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `uint-to-list-u64` expands a bitmap into a 64-element list. Reach it through `liquidate` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `min-collateral-expected` varied, and assert that the value `uint-to-list-u64` returns is identical in both runs; a divergence confirms the finding.
