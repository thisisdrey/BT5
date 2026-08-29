# Q5420: resolve-interpolation-points via repay: make a health check read a different position than the one

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `amount`, including far above the real debt (the capping path) reach `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) in a state where it make a health check read a different position than the one that will exist? Given that it selects the bracketing curve points for a utilization, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `repay` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with `amount`, including far above the real debt (the capping path) varied, and assert that the value `resolve-interpolation-points` returns is identical in both runs; a divergence confirms the finding.
