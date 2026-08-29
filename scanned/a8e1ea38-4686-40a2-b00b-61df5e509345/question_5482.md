# Q5482: total-debt via accrue: make an aggregate and its per-item breakdown disagree

## Question
Entering through `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) while controlling whether an earlier call in the same block already advanced last-update, can an unprivileged attacker make `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) make an aggregate and its per-item breakdown disagree? `total-debt` computes cumulative debt from `principal-scaled` and `index`, so the invariant that only the acting principal's own position is mutated would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: whether an earlier call in the same block already advanced last-update
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `accrue` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `accrue` with whether an earlier call in the same block already advanced last-update, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
