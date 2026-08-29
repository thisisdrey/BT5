# Q1709: send-tokens via collateral-remove: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), controlling the set of assets held, drive `send-tokens` (mainnet/contracts/market/v0-market-vault.clar:259) — which pushes an asset to a caller-chosen recipient principal — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that no position row exists that the position mask does not represent, and cause permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:259` -> `send-tokens`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `send-tokens` pushes an asset to a caller-chosen recipient principal. Reach it through `collateral-remove` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `collateral-remove` call, then the attacker-shaped one with the set of assets held, and assert the attacker's net token balance change is zero or negative.
