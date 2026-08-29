# Q1820: debt-preview via deposit: make an aggregate and its per-item breakdown disagree

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `amount` reach `debt-preview` (mainnet/contracts/vault/v0-vault-stx.clar:331) in a state where it make an aggregate and its per-item breakdown disagree? Given that it computes cumulative debt from `principal-scaled` and the FORWARD index, the invariant that conversions never round in the user's favour in either direction breaks and the result is protocol insolvency.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:331` -> `debt-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `debt-preview` computes cumulative debt from `principal-scaled` and the FORWARD index. Reach it through `deposit` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `deposit` twice with `amount` varied, and assert that the value `debt-preview` returns is identical in both runs; a divergence confirms the finding.
