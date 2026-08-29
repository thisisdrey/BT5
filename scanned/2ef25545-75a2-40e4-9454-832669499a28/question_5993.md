# Q5993: find-and-resolve-asset-value via liquidate-redeem: satisfy a bound with a value the bound was never designed 

## Question
Can an unprivileged attacker entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), controlling the vault whose share price the redemption moves, drive `find-and-resolve-asset-value` (mainnet/contracts/market/v0-4-market.clar:668) — which reuses an already-resolved price from the asset list and returns u0 when the asset is not found — to satisfy a bound with a value the bound was never designed to admit, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:668` -> `find-and-resolve-asset-value`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `find-and-resolve-asset-value` reuses an already-resolved price from the asset list and returns u0 when the asset is not found. Reach it through `liquidate-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `liquidate-redeem` call, then the attacker-shaped one with the vault whose share price the redemption moves, and assert the attacker's net token balance change is zero or negative.
