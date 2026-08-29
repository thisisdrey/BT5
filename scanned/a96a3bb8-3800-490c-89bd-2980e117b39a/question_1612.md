# Q1612: total-debt via transfer: make an aggregate and its per-item breakdown disagree

## Question
Does `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752) let an unprivileged attacker who controls `amount` reach `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) in a state where it make an aggregate and its per-item breakdown disagree? Given that it computes cumulative debt from `principal-scaled` and `index`, the invariant that conversions never round in the user's favour in either direction breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `transfer` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `transfer` with `amount`, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
