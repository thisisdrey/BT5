# Q5600: get-asset-value via supply-collateral-add: make a health check read a different position than the one

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls the position state the final collateral-add is validated against reach `get-asset-value` (mainnet/contracts/market/v0-4-market.clar:679) in a state where it make a health check read a different position than the one that will exist? Given that it resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:679` -> `get-asset-value`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the position state the final collateral-add is validated against
- Exploit idea: `get-asset-value` resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with the position state the final collateral-add is validated against varied, and assert that the value `get-asset-value` returns is identical in both runs; a divergence confirms the finding.
