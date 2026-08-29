# Q2984: get-position via liquidate: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382) let an unprivileged attacker who controls which collateral and debt asset pair is targeted reach `get-position` (mainnet/contracts/market/v0-4-market.clar:466) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it returns only rows whose bit is set in the ENABLED bitmap, the invariant that only the acting principal's own position is mutated breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:466` -> `get-position`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `get-position` returns only rows whose bit is set in the ENABLED bitmap. Reach it through `liquidate` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `liquidate` twice with which collateral and debt asset pair is targeted varied, and assert that the value `get-position` returns is identical in both runs; a divergence confirms the finding.
