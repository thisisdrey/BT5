# Q1001: get-egroup via supply-collateral-add: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175), controlling `min-shares` (the only slippage bound on the deposit leg), drive `get-egroup` (mainnet/contracts/market/v0-4-market.clar:460) — which resolves the efficiency group for a mask and is unwrapped with `try!` on every health path — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:460` -> `get-egroup`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `min-shares` (the only slippage bound on the deposit leg)
- Exploit idea: `get-egroup` resolves the efficiency group for a mask and is unwrapped with `try!` on every health path. Reach it through `supply-collateral-add` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `supply-collateral-add` call, then the attacker-shaped one with `min-shares` (the only slippage bound on the deposit leg), and assert the attacker's net token balance change is zero or negative.
