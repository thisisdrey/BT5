# Q1822: relevant via liquidate-multi: make a health check read a different position than the one

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling which borrowers are placed early versus late in the batch, can an unprivileged attacker make `relevant` (mainnet/contracts/market/v0-market-vault.clar:175) make a health check read a different position than the one that will exist? `relevant` drops any position row whose bit is not present in the enabled mask, so the invariant that only the acting principal's own position is mutated would fail, yielding direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:175` -> `relevant`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: which borrowers are placed early versus late in the batch
- Exploit idea: `relevant` drops any position row whose bit is not present in the enabled mask. Reach it through `liquidate-multi` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `liquidate-multi` with which borrowers are placed early versus late in the batch, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
