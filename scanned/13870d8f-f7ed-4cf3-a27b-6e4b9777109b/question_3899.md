# Q3899: resolve-ststx via collateral-remove-redeem: make an aggregate and its per-item breakdown disagree

## Question
`resolve-ststx` (mainnet/contracts/market/v0-4-market.clar:339) calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down`. Can an unprivileged caller of `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211), by choosing `min-underlying`, use that to make an aggregate and its per-item breakdown disagree, violating the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:339` -> `resolve-ststx`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `min-underlying`
- Exploit idea: `resolve-ststx` calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down`. Reach it through `collateral-remove-redeem` and make an aggregate and its per-item breakdown disagree.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `collateral-remove-redeem` call, then the attacker-shaped one with `min-underlying`, and assert the attacker's net token balance change is zero or negative.
