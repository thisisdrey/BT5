# Q3918: total-assets-preview via deposit: satisfy a bound with a value the bound was never designed 

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling `min-out`, can an unprivileged attacker make `total-assets-preview` (mainnet/contracts/vault/v0-vault-stx.clar:341) satisfy a bound with a value the bound was never designed to admit? `total-assets-preview` re-derives a FORWARD index inside calls that have already accrued, so the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:341` -> `total-assets-preview`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `total-assets-preview` re-derives a FORWARD index inside calls that have already accrued. Reach it through `deposit` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `min-out` across its boundary values through `deposit` in simnet and assert `total-assets-preview` never returns a value that breaks the invariant.
