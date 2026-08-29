# Q5120: get-asset-value via liquidate: make a health check read a different position than the one

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls `borrower`, any third-party principal reach `get-asset-value` (mainnet/contracts/market/v0-4-market.clar:679) in a state where it make a health check read a different position than the one that will exist? Given that it resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:679` -> `get-asset-value`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: `borrower`, any third-party principal
- Exploit idea: `get-asset-value` resolves a fresh price for a single asset and normalizes with a caller-supplied rounding direction. Reach it through `liquidate` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with `borrower`, any third-party principal varied, and assert that the value `get-asset-value` returns is identical in both runs; a divergence confirms the finding.
