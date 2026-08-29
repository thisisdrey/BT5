# Q1597: resolve-interpolation-points via redeem: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797), controlling `recipient`, drive `resolve-interpolation-points` (mainnet/contracts/vault/v0-vault-stx.clar:205) — which selects the bracketing curve points for a utilization — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that no position row exists that the position mask does not represent, and cause direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:205` -> `resolve-interpolation-points`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `recipient`
- Exploit idea: `resolve-interpolation-points` selects the bracketing curve points for a utilization. Reach it through `redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `redeem` with `recipient`, then read `resolve-interpolation-points` state before and after in the same block and assert the two sides of the invariant are equal.
