# Q3297: remove-user-collateral via supply-collateral-add: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), controlling the position state the final collateral-add is validated against, drive `remove-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:205) — which asserts sufficiency then `map-delete`s only on an exact zero — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:205` -> `remove-user-collateral`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the position state the final collateral-add is validated against
- Exploit idea: `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `remove-user-collateral` touches, run `supply-collateral-add` with the position state the final collateral-add is validated against, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
