# Q0812: send-underlying via redeem: satisfy a bound with a value the bound was never designed 

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls `min-out` reach `send-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:296) in a state where it satisfy a bound with a value the bound was never designed to admit? Given that it pushes the underlying under an `as-contract?` post-condition scope, the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:296` -> `send-underlying`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `send-underlying` pushes the underlying under an `as-contract?` post-condition scope. Reach it through `redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `redeem` twice with `min-out` varied, and assert that the value `send-underlying` returns is identical in both runs; a divergence confirms the finding.
