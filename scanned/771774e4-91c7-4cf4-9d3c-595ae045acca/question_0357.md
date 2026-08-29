# Q0357: resolve-price-feed via liquidate-multi: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), controlling the full batch list and its ordering, drive `resolve-price-feed` (mainnet/contracts/market/v0-4-market.clar:332) — which dispatches on a 1-byte type to `resolve-pyth` or `resolve-dia`, erroring otherwise — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV, and cause permanent freezing of a position that can never be closed?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:332` -> `resolve-price-feed`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `resolve-price-feed` dispatches on a 1-byte type to `resolve-pyth` or `resolve-dia`, erroring otherwise. Reach it through `liquidate-multi` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Snapshot every state variable `resolve-price-feed` touches, run `liquidate-multi` with the full batch list and its ordering, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
