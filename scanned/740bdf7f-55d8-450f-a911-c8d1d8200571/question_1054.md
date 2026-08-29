# Q1054: merge-price via liquidate-redeem: make a health check read a different position than the one

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the vault whose share price the redemption moves, can an unprivileged attacker make `merge-price` (mainnet/contracts/market/v0-4-market.clar:506) make a health check read a different position than the one that will exist? `merge-price` attaches a price to an asset record by position in the fold, not by asset id, so the invariant that only the acting principal's own position is mutated would fail, yielding direct theft of user funds at rest or in motion.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:506` -> `merge-price`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `merge-price` attaches a price to an asset record by position in the fold, not by asset id. Reach it through `liquidate-redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: Set up the position in simnet, call `liquidate-redeem` with the vault whose share price the redemption moves, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
