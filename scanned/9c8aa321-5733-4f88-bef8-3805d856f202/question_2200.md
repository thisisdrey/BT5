# Q2200: write-feed via liquidate-redeem: make two code sites that must agree disagree by an attacke

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the redemption receiver reach `write-feed` (mainnet/contracts/market/v0-4-market.clar:129) in a state where it make two code sites that must agree disagree by an attacker-chosen amount? Given that it applies one Pyth price-feed update and folds its status, the invariant that every asset a position holds enters the health evaluation exactly once breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:129` -> `write-feed`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the redemption receiver
- Exploit idea: `write-feed` applies one Pyth price-feed update and folds its status. Reach it through `liquidate-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `liquidate-redeem` with the redemption receiver, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
