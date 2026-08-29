# Q1349: write-feed via supply-collateral-add: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), controlling the position state the final collateral-add is validated against, drive `write-feed` (mainnet/contracts/market/v0-4-market.clar:129) — which applies one Pyth price-feed update and folds its status — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that no position row exists that the position mask does not represent, and cause permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:129` -> `write-feed`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: the position state the final collateral-add is validated against
- Exploit idea: `write-feed` applies one Pyth price-feed update and folds its status. Reach it through `supply-collateral-add` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `supply-collateral-add` call, then the attacker-shaped one with the position state the final collateral-add is validated against, and assert the attacker's net token balance change is zero or negative.
