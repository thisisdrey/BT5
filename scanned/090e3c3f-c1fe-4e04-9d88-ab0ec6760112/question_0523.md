# Q0523: calc-multiplier-delta via accrue: compose two individually correct mechanisms into an incorr

## Question
`calc-multiplier-delta` (mainnet/contracts/vault/v0-vault-stx.clar:170) compounds a rate over `time-delta` with a caller-independent rounding flag. Can an unprivileged caller of `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), by choosing the block time at which accrual is first triggered in a block, use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that a value cached within a block still describes the state it was derived from and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:170` -> `calc-multiplier-delta`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `calc-multiplier-delta` compounds a rate over `time-delta` with a caller-independent rounding flag. Reach it through `accrue` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `accrue` with the block time at which accrual is first triggered in a block, then read `calc-multiplier-delta` state before and after in the same block and assert the two sides of the invariant are equal.
