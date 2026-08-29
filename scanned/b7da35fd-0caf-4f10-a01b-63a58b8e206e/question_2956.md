# Q2956: merge-price via call-ststx-ratio: reach a state the guard immediately upstream of it never c

## Question
Does `call-ststx-ratio` (mainnet/contracts/market/v0-4-market.clar:1015) let an unprivileged attacker who controls the block and transaction position at which the external ratio is fetched reach `merge-price` (mainnet/contracts/market/v0-4-market.clar:506) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it attaches a price to an asset record by position in the fold, not by asset id, the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:506` -> `merge-price`
- Entrypoint: `call-ststx-ratio` (`mainnet/contracts/market/v0-4-market.clar:1015`), unprivileged and publicly callable
- Attacker controls: the block and transaction position at which the external ratio is fetched
- Exploit idea: `merge-price` attaches a price to an asset record by position in the fold, not by asset id. Reach it through `call-ststx-ratio` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `call-ststx-ratio` with the block and transaction position at which the external ratio is fetched, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
