# Q5668: iter-lookup-collateral via liquidate-multi: make a health check read a different position than the one

## Question
Does `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) let an unprivileged attacker who controls the full batch list and its ordering reach `iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) in a state where it make a health check read a different position than the one that will exist? Given that it skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position, the invariant that a value cached within a block still describes the state it was derived from breaks and the result is theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `liquidate-multi` and make a health check read a different position than the one that will exist.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `liquidate-multi` with the full batch list and its ordering, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
