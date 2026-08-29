# Q5983: get-bitmap via borrow: convert a rounding direction into a repeatable extraction

## Question
`get-bitmap` (mainnet/contracts/registry/v0-assets.clar:145) returns the global enabled bitmap that every position read filters on. Can an unprivileged caller of `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), by choosing `amount`, use that to convert a rounding direction into a repeatable extraction, violating the invariant that only the acting principal's own position is mutated and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/registry/v0-assets.clar:145` -> `get-bitmap`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `get-bitmap` returns the global enabled bitmap that every position read filters on. Reach it through `borrow` and convert a rounding direction into a repeatable extraction.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `borrow` with `amount`, then read `get-bitmap` state before and after in the same block and assert the two sides of the invariant are equal.
