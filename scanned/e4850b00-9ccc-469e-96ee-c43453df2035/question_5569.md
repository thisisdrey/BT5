# Q5569: iter-lookup-collateral via collateral-add: turn an accounting residue into a permanently unclosable p

## Question
Can an unprivileged attacker entering through `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), controlling call ordering within the block, drive `iter-lookup-collateral` (mainnet/contracts/market/v0-market-vault.clar:180) — which skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position — to turn an accounting residue into a permanently unclosable position, breaking the invariant that no position row exists that the position mask does not represent, and cause theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:180` -> `iter-lookup-collateral`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: call ordering within the block
- Exploit idea: `iter-lookup-collateral` skips rows failing `relevant`, so a disabled asset's collateral vanishes from the returned position. Reach it through `collateral-add` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-add` with call ordering within the block, then read `iter-lookup-collateral` state before and after in the same block and assert the two sides of the invariant are equal.
