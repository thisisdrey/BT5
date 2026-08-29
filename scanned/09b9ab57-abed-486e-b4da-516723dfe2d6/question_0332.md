# Q0332: calc-utilization via repay: satisfy a bound with a value the bound was never designed 

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls whether the repaid asset is in the accrued debt list reach `calc-utilization` (mainnet/contracts/vault/v0-vault-stx.clar:164) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it divides debt by available liquidity, which can exceed BPS when debt outruns assets, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:164` -> `calc-utilization`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: whether the repaid asset is in the accrued debt list
- Exploit idea: `calc-utilization` divides debt by available liquidity, which can exceed BPS when debt outruns assets. Reach it through `repay` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `repay` twice with whether the repaid asset is in the accrued debt list varied, and assert that the value `calc-utilization` returns is identical in both runs; a divergence confirms the finding.
