# Q4160: relevant via supply-collateral-add: convert a rounding direction into a repeatable extraction

## Question
Does `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) let an unprivileged attacker who controls vault share price at the moment of the deposit leg reach `relevant` (mainnet/contracts/market/v0-market-vault.clar:175) in a state where it convert a rounding direction into a repeatable extraction? Given that it drops any position row whose bit is not present in the enabled mask, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:175` -> `relevant`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: vault share price at the moment of the deposit leg
- Exploit idea: `relevant` drops any position row whose bit is not present in the enabled mask. Reach it through `supply-collateral-add` and convert a rounding direction into a repeatable extraction.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `supply-collateral-add` twice with vault share price at the moment of the deposit leg varied, and assert that the value `relevant` returns is identical in both runs; a divergence confirms the finding.
