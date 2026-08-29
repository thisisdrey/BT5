# Q1704: interpolate-rate via borrow: make an aggregate and its per-item breakdown disagree

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls `amount` reach `interpolate-rate` (mainnet/contracts/vault/v0-vault-stx.clar:196) in a state where it make an aggregate and its per-item breakdown disagree? Given that it interpolates between packed u16 curve points, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:196` -> `interpolate-rate`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `interpolate-rate` interpolates between packed u16 curve points. Reach it through `borrow` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz `amount` across its boundary values through `borrow` in simnet and assert `interpolate-rate` never returns a value that breaks the invariant.
