# Q3673: total-debt via deposit: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763), controlling `amount`, drive `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) — which computes cumulative debt from `principal-scaled` and `index` — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `deposit` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `deposit` with `amount`, then read `total-debt` state before and after in the same block and assert the two sides of the invariant are equal.
