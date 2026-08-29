# Q1436: mask-shift-combine via liquidate: make an aggregate and its per-item breakdown disagree

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `min-collateral-expected` reach `mask-shift-combine` (mainnet/contracts/market/v0-4-market.clar:422) in a state where it make an aggregate and its per-item breakdown disagree? Given that it folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:422` -> `mask-shift-combine`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `min-collateral-expected`
- Exploit idea: `mask-shift-combine` folds the 128-bit mask down by shifting the debt half by DEBT-OFFSET and OR-ing it onto the collateral half. Reach it through `liquidate` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `min-collateral-expected` varied, and assert that the value `mask-shift-combine` returns is identical in both runs; a divergence confirms the finding.
