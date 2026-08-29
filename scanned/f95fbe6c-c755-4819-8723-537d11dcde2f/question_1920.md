# Q1920: system-borrow via borrow: make an aggregate and its per-item breakdown disagree

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls `amount` reach `system-borrow` (mainnet/contracts/vault/v0-vault-stx.clar:865) in a state where it make an aggregate and its per-item breakdown disagree? Given that it independently computes `scaled-amount` with `mul-div-up` from its own `index`, duplicating the market's own scaling of the same borrow, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:865` -> `system-borrow`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `system-borrow` independently computes `scaled-amount` with `mul-div-up` from its own `index`, duplicating the market's own scaling of the same borrow. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `borrow` in simnet and assert `system-borrow` never returns a value that breaks the invariant.
