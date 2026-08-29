# Q5148: resolve-interpolation-points via collateral-remove: make a health check read a different position than the one

## Question
Does `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107) let an unprivileged attacker who controls the `price-feeds` buffers reach `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) in a state where it make a health check read a different position than the one that will exist? Given that it selects the bracketing curve points for a utilization, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is temporary freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `collateral-remove` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - temporary freezing of funds
- Fast validation: Fuzz the `price-feeds` buffers across its boundary values through `collateral-remove` in simnet and assert `resolve-interpolation-points` never returns a value that breaks the invariant.
