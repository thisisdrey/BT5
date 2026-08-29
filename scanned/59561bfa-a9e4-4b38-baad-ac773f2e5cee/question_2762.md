# Q2762: filter-u128 via liquidate: compose two individually correct mechanisms into an incorr

## Question
Entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) while controlling `collateral-receiver`, can an unprivileged attacker make `filter-u128` (mainnet/contracts/registry/v0-egroup.clar:97) compose two individually correct mechanisms into an incorrect result? `filter-u128` filters a 128-entry bucket list, so the invariant that no position row exists that the position mask does not represent would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/registry/v0-egroup.clar:97` -> `filter-u128`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `collateral-receiver`
- Exploit idea: `filter-u128` filters a 128-entry bucket list. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `collateral-receiver` varied, and assert that the value `filter-u128` returns is identical in both runs; a divergence confirms the finding.
