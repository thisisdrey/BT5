# Q5887: mask-update via liquidate-multi: convert a rounding direction into a repeatable extraction

## Question
`mask-update` (mainnet/contracts/market/v0-market-vault.clar:94) sets or clears one bit, clearing only when the row reaches exactly zero. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing the full batch list and its ordering, use that to convert a rounding direction into a repeatable extraction, violating the invariant that only the acting principal's own position is mutated and producing direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:94` -> `mask-update`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `mask-update` sets or clears one bit, clearing only when the row reaches exactly zero. Reach it through `liquidate-multi` and convert a rounding direction into a repeatable extraction.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-multi` with the full batch list and its ordering, then read `mask-update` state before and after in the same block and assert the two sides of the invariant are equal.
