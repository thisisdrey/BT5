# Q1020: linear-interpolate via deposit: make an aggregate and its per-item breakdown disagree

## Question
Does `deposit` (mainnet/contracts/vault/v0-vault-stx.clar:763) let an unprivileged attacker who controls `amount` reach `linear-interpolate` (mainnet/contracts/vault/v0-vault-stx.clar:221) in a state where it make an aggregate and its per-item breakdown disagree? Given that it interpolates between two points, dividing by `(- x2 x1)`, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:221` -> `linear-interpolate`
- Entrypoint: `deposit` (`mainnet/contracts/vault/v0-vault-stx.clar:763`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `linear-interpolate` interpolates between two points, dividing by `(- x2 x1)`. Reach it through `deposit` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `deposit` in simnet and assert `linear-interpolate` never returns a value that breaks the invariant.
