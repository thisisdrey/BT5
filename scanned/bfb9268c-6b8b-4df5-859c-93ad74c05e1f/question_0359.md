# Q0359: get-egroup via collateral-add: compose two individually correct mechanisms into an incorr

## Question
`get-egroup` (mainnet/contracts/market/v0-4-market.clar:460) resolves the efficiency group for a mask and is unwrapped with `try!` on every health path. Can an unprivileged caller of `collateral-add` (mainnet/contracts/market/v0-4-market.clar:1020), by choosing whether this asset is already collateral (the is-new-collateral branch), use that to compose two individually correct mechanisms into an incorrect result, violating the invariant that conversions never round in the user's favour in either direction and producing permanent freezing of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:460` -> `get-egroup`
- Entrypoint: `collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1020`), unprivileged and publicly callable
- Attacker controls: whether this asset is already collateral (the is-new-collateral branch)
- Exploit idea: `get-egroup` resolves the efficiency group for a mask and is unwrapped with `try!` on every health path. Reach it through `collateral-add` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Run the baseline `collateral-add` call, then the attacker-shaped one with whether this asset is already collateral (the is-new-collateral branch), and assert the attacker's net token balance change is zero or negative.
