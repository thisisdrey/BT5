# Q4593: find-collateral-amount via liquidate-multi: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `liquidate-multi` (mainnet/contracts/market/v0-4-market.clar:1593), controlling the full batch list and its ordering, drive `find-collateral-amount` (mainnet/contracts/market/v0-4-market.clar:609) — which returns u0 for an absent asset, making a missing row indistinguishable from a zero holding — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV, and cause permanent freezing of funds?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:609` -> `find-collateral-amount`
- Entrypoint: `liquidate-multi` (`mainnet/contracts/market/v0-4-market.clar:1593`), unprivileged and publicly callable
- Attacker controls: the full batch list and its ordering
- Exploit idea: `find-collateral-amount` returns u0 for an absent asset, making a missing row indistinguishable from a zero holding. Reach it through `liquidate-multi` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Snapshot every state variable `find-collateral-amount` touches, run `liquidate-multi` with the full batch list and its ordering, recompute the invariant off-chain from the snapshot, and assert it matches the on-chain result.
