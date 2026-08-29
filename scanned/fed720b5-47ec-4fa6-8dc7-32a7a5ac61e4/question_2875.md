# Q2875: accrue-collateral-asset via accrue: satisfy a bound with a value the bound was never designed 

## Question
`accrue-collateral-asset` (mainnet/contracts/market/v0-4-market.clar:273) maps a ztoken id to a vault id through a chain of `is-eq` tests that falls through to the u100 sentinel. Can an unprivileged caller of `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), by choosing whether an earlier call in the same block already advanced last-update, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:273` -> `accrue-collateral-asset`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: whether an earlier call in the same block already advanced last-update
- Exploit idea: `accrue-collateral-asset` maps a ztoken id to a vault id through a chain of `is-eq` tests that falls through to the u100 sentinel. Reach it through `accrue` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `accrue` with whether an earlier call in the same block already advanced last-update, then read `accrue-collateral-asset` state before and after in the same block and assert the two sides of the invariant are equal.
