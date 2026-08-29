# Q4496: relevant via collateral-remove: convert a rounding direction into a repeatable extraction

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the set of assets held reach `relevant` (mainnet/contracts/market/v0-market-vault.clar:175) in a state where it convert a rounding direction into a repeatable extraction? Given that it drops any position row whose bit is not present in the enabled mask, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency through uncollateralised debt.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:175` -> `relevant`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `relevant` drops any position row whose bit is not present in the enabled mask. Reach it through `collateral-remove` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Write a Clarinet simnet test calling `collateral-remove` twice with the set of assets held varied, and assert that the value `relevant` returns is identical in both runs; a divergence confirms the finding.
