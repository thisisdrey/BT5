# Q2438: get-position via supply-collateral-add: compose two individually correct mechanisms into an incorr

## Question
Entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) while controlling the position state the final collateral-add is validated against, can an unprivileged attacker make `get-position` (mainnet/contracts/market/v0-4-market.clar:466) compose two individually correct mechanisms into an incorrect result? `get-position` returns only rows whose bit is set in the ENABLED bitmap, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:466` -> `get-position`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the position state the final collateral-add is validated against
- Exploit idea: `get-position` returns only rows whose bit is set in the ENABLED bitmap. Reach it through `supply-collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with the position state the final collateral-add is validated against varied, and assert that the value `get-position` returns is identical in both runs; a divergence confirms the finding.
