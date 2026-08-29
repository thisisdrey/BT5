# Q1310: mask-pos via supply-collateral-add: make a health check read a different position than the one

## Question
Entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) while controlling `amount`, can an unprivileged attacker make `mask-pos` (mainnet/contracts/market/v0-market-vault.clar:91) make a health check read a different position than the one that will exist? `mask-pos` maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET, so the invariant that only the acting principal's own position is mutated would fail, yielding protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:91` -> `mask-pos`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `mask-pos` maps an asset id to a bit position, offsetting debt bits by DEBT-OFFSET. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with `amount` varied, and assert that the value `mask-pos` returns is identical in both runs; a divergence confirms the finding.
