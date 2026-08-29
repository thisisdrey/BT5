# Q1644: vault-accrue via repay: make an aggregate and its per-item breakdown disagree

## Question
Does `repay` (mainnet/contracts/market/v0-4-market.clar:1316) let an unprivileged attacker who controls `amount`, including far above the real debt (the capping path) reach `vault-accrue` (mainnet/contracts/market/v0-4-market.clar:189) in a state where it make an aggregate and its per-item breakdown disagree? Given that it dispatches accrual to one of six vaults by asset id, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:189` -> `vault-accrue`
- Entrypoint: `repay` (`mainnet/contracts/market/v0-4-market.clar:1316`), unprivileged and publicly callable
- Attacker controls: `amount`, including far above the real debt (the capping path)
- Exploit idea: `vault-accrue` dispatches accrual to one of six vaults by asset id. Reach it through `repay` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `amount`, including far above the real debt (the capping path) across its boundary values through `repay` in simnet and assert `vault-accrue` never returns a value that breaks the invariant.
