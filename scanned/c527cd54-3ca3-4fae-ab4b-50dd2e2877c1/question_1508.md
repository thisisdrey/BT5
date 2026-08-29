# Q1508: find-debt-scaled via collateral-add: make an aggregate and its per-item breakdown disagree

## Question
Does `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020) let an unprivileged attacker who controls the three `price-feeds` buffers and their order reach `find-debt-scaled` (mainnet/contracts/market/v0-4-market.clar:621) in a state where it make an aggregate and its per-item breakdown disagree? Given that it returns u0 for an absent asset, making a missing debt row indistinguishable from no debt, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:621` -> `find-debt-scaled`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: the three `price-feeds` buffers and their order
- Exploit idea: `find-debt-scaled` returns u0 for an absent asset, making a missing debt row indistinguishable from no debt. Reach it through `collateral-add` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-add` twice with the three `price-feeds` buffers and their order varied, and assert that the value `find-debt-scaled` returns is identical in both runs; a divergence confirms the finding.
