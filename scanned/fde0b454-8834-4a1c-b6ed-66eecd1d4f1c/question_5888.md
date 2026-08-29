# Q5888: receive-underlying via accrue: compose two individually correct mechanisms into an incorr

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls the utilization the rate is interpolated at reach `receive-underlying` (mainnet/contracts/vault/v0-vault-stx.clar:291) in a state where it compose two individually correct mechanisms into an incorrect result? Given that it pulls the underlying from a named account, the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:291` -> `receive-underlying`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the utilization the rate is interpolated at
- Exploit idea: `receive-underlying` pulls the underlying from a named account. Reach it through `accrue` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `accrue` twice with the utilization the rate is interpolated at varied, and assert that the value `receive-underlying` returns is identical in both runs; a divergence confirms the finding.
