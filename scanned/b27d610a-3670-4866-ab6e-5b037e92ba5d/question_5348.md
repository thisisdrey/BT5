# Q5348: vault-accrue via deposit: make a health check read a different position than the one

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `recipient`, including a contract principal reach `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) in a state where it make a health check read a different position than the one that will exist? Given that it dispatches accrual to one of six vaults by asset id, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `deposit` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `deposit` twice with `recipient`, including a contract principal varied, and assert that the value `vault-accrue` returns is identical in both runs; a divergence confirms the finding.
