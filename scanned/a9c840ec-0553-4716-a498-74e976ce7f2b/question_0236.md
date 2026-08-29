# Q0236: debt-preview via liquidate-redeem: satisfy a bound with a value the bound was never designed 

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the borrower targeted reach `debt-preview` (mainnet/contracts/vault/v0-vault-stx.clar:331) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it computes cumulative debt from `principal-scaled` and the FORWARD index, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:331` -> `debt-preview`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the borrower targeted
- Exploit idea: `debt-preview` computes cumulative debt from `principal-scaled` and the FORWARD index. Reach it through `liquidate-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the borrower targeted varied, and assert that the value `debt-preview` returns is identical in both runs; a divergence confirms the finding.
