# Q2950: mask-update via liquidate-multi: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593) while controlling the full batch list and its ordering, can an unprivileged attacker make `mask-update` (mainnet/contracts/market/v0-market-vault.clar:94) turn an accounting residue into a permanently unclosable position? `mask-update` sets or clears one bit, clearing only when the row reaches exactly zero, so the invariant that conversions never round in the user's favour in either direction would fail, yielding theft of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:94` -> `mask-update`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `mask-update` sets or clears one bit, clearing only when the row reaches exactly zero. Reach it through `liquidate-multi` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: Set up the position in simnet, call `liquidate-multi` with the full batch list and its ordering, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
