# Q1432: vault-accrue via deposit: make an aggregate and its per-item breakdown disagree

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `recipient`, including a contract principal reach `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) in a state where it make an aggregate and its per-item breakdown disagree? Given that it dispatches accrual to one of six vaults by asset id, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `recipient`, including a contract principal
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `deposit` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `deposit` with `recipient`, including a contract principal, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
