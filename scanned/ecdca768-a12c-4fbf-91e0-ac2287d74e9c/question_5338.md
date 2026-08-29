# Q5338: interpolate-rate via deposit: make an aggregate and its per-item breakdown disagree

## Question
Entering through `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) while controlling `recipient`, including a contract principal, can an unprivileged attacker make `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) make an aggregate and its per-item breakdown disagree? `interpolate-rate` interpolates between packed u16 curve points, so the invariant that only the acting principal's own position is mutated would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `deposit` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `deposit` with `recipient`, including a contract principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
