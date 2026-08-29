# Q1596: total-assets via redeem: make an aggregate and its per-item breakdown disagree

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls `min-out` reach `total-assets` (mainnet/contracts/vault/v0-vault-stx.clar:334) in a state where it make an aggregate and its per-item breakdown disagree? Given that it adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:334` -> `total-assets`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `min-out`
- Exploit idea: `total-assets` adds `(- debt borrowed)` as accrued interest that no token in the vault yet backs. Reach it through `redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `min-out` across its boundary values through `redeem` in simnet and assert `total-assets` never returns a value that breaks the invariant.
