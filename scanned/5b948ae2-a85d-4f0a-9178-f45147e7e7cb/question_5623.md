# Q5623: remove-user-collateral via transfer: reach a state the guard immediately upstream of it never c

## Question
`remove-user-collateral` (mainnet/contracts/market/v0-market-vault.clar:205) asserts sufficiency then `map-delete`s only on an exact zero. Can an unprivileged caller of `transfer` (mainnet/contracts/vault/v0-vault-stx.clar:752), by choosing the timing relative to a pledge or a liquidation, use that to reach a state the guard immediately upstream of it never contemplated, violating the invariant that every asset a position holds enters the health evaluation exactly once and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:205` -> `remove-user-collateral`
- Entrypoint: `transfer` (`mainnet/contracts/vault/v0-vault-stx.clar:752`), unprivileged and publicly callable
- Attacker controls: the timing relative to a pledge or a liquidation
- Exploit idea: `remove-user-collateral` asserts sufficiency then `map-delete`s only on an exact zero. Reach it through `transfer` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `transfer` with the timing relative to a pledge or a liquidation, then read `remove-user-collateral` state before and after in the same block and assert the two sides of the invariant are equal.
