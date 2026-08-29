# Q1048: interest-rate via accrue: make an aggregate and its per-item breakdown disagree

## Question
Does `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835) let an unprivileged attacker who controls the utilization the rate is interpolated at reach `interest-rate` (mainnet/contracts/vault/v0-vault-stx.clar:371) in a state where it make an aggregate and its per-item breakdown disagree? Given that it interpolates the packed curve at the current utilization, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:371` -> `interest-rate`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the utilization the rate is interpolated at
- Exploit idea: `interest-rate` interpolates the packed curve at the current utilization. Reach it through `accrue` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `accrue` with the utilization the rate is interpolated at, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
