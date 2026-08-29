# Q2731: iter-lookup-collateral via liquidate-multi: satisfy a bound with a value the bound was never designed 

## Question
`iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Can an unprivileged caller of `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), by choosing the full batch list and its ordering, use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `liquidate-multi` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-multi` with the full batch list and its ordering, then read `iter-lookup-collateral` state before and after in the same block and assert the two sides of the invariant are equal.
