# Q5885: price-multi-resolve via borrow: satisfy a bound with a value the bound was never designed 

## Question
Can an unprivileged attacker entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), controlling the future mask produced by the new debt bit, drive `price-multi-resolve` (mainnet/contracts/market/v0-4-market.clar:397) — which folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end — to satisfy a bound with a value the bound was never designed to admit, breaking the invariant that conversions never round in the user's favour in either direction, and cause protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:397` -> `price-multi-resolve`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: the future mask produced by the new debt bit
- Exploit idea: `price-multi-resolve` folds `iter-price-multi` into a POSITIONAL price list, asserting only the `valid` flag at the end. Reach it through `borrow` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `borrow` call, then the attacker-shaped one with the future mask produced by the new debt bit, and assert the attacker's net token balance change is zero or negative.
