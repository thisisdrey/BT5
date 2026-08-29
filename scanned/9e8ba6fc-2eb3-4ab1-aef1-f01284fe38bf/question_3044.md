# Q3044: linear-interpolate via repay: reach a state the guard immediately upstream of it never c

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls the `ft` trait principal reach `linear-interpolate` (mainnet/contracts/vault/v0-vault-stx.clar:221) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it interpolates between two points, dividing by `(- x2 x1)`, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:221` -> `linear-interpolate`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: the `ft` trait principal
- Exploit idea: `linear-interpolate` interpolates between two points, dividing by `(- x2 x1)`. Reach it through `repay` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with the `ft` trait principal varied, and assert that the value `linear-interpolate` returns is identical in both runs; a divergence confirms the finding.
