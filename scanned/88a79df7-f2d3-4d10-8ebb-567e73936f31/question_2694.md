# Q2694: total-debt via deposit: compose two individually correct mechanisms into an incorr

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling `amount`, can an unprivileged attacker make `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) compose two individually correct mechanisms into an incorrect result? `total-debt` computes cumulative debt from `principal-scaled` and `index`, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `deposit` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `deposit` in simnet and assert `total-debt` never returns a value that breaks the invariant.
