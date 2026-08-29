# Q4980: resolve-interpolation-points via call-ststx-ratio: make a health check read a different position than the one

## Question
Does `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) let an unprivileged attacker who controls the block and transaction position at which the external ratio is fetched reach `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) in a state where it make a health check read a different position than the one that will exist? Given that it selects the bracketing curve points for a utilization, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: the block and transaction position at which the external ratio is fetched
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `call-ststx-ratio` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the block and transaction position at which the external ratio is fetched across its boundary values through `call-ststx-ratio` in simnet and assert `resolve-interpolation-points` never returns a value that breaks the invariant.
