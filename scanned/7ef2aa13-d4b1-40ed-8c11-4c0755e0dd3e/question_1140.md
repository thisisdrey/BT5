# Q1140: total-debt via redeem: make an aggregate and its per-item breakdown disagree

## Question
Does `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797) let an unprivileged attacker who controls `amount` of shares burned reach `total-debt` (mainnet/contracts/vault/v0-vault-stx.clar:328) in a state where it make an aggregate and its per-item breakdown disagree? Given that it computes cumulative debt from `principal-scaled` and `index`, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:328` -> `total-debt`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `amount` of shares burned
- Exploit idea: `total-debt` computes cumulative debt from `principal-scaled` and `index`. Reach it through `redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `amount` of shares burned across its boundary values through `redeem` in simnet and assert `total-debt` never returns a value that breaks the invariant.
