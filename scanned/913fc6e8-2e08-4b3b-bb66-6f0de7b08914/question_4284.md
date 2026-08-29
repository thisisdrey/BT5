# Q4284: resolve-interpolation-points via borrow: convert a rounding direction into a repeatable extraction

## Question
Does `borrow` (mainnet/contracts/market/v0-4-market.clar:1238) let an unprivileged attacker who controls the `price-feeds` buffers reach `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) in a state where it convert a rounding direction into a repeatable extraction? Given that it selects the bracketing curve points for a utilization, the invariant that no position row exists that the position mask does not represent breaks and the result is permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the `price-feeds` buffers
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `borrow` and convert a rounding direction into a repeatable extraction.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz the `price-feeds` buffers across its boundary values through `borrow` in simnet and assert `resolve-interpolation-points` never returns a value that breaks the invariant.
