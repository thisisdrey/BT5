# Q2419: increment via liquidate-multi: satisfy a bound with a value the bound was never designed 

## Question
`increment` (mainnet/contracts/market/v0-market-vault.clar:137) advances the user-id nonce. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing the full batch list and its ordering, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:137` -> `increment`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `increment` advances the user-id nonce. Reach it through `liquidate-multi` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-multi` with the full batch list and its ordering, then read `increment` state before and after in the same block and assert the two sides of the invariant are equal.
