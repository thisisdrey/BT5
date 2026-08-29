# Q2747: user-safe-mask via collateral-remove: satisfy a bound with a value the bound was never designed 

## Question
`user-safe-mask` (mainnet/contracts/market/v0-4-market.clar:428) ANDs the user's collateral bits against the enabled bitmap but keeps ALL debt bits unfiltered. Can an unprivileged caller of `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), by choosing `amount` relative to the current collateral row (the removing-all branch), use that to satisfy a bound with a value the bound was never designed to admit, violating the invariant that only the acting principal's own position is mutated and producing protocol insolvency?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:428` -> `user-safe-mask`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: `amount` relative to the current collateral row (the removing-all branch)
- Exploit idea: `user-safe-mask` ANDs the user's collateral bits against the enabled bitmap but keeps ALL debt bits unfiltered. Reach it through `collateral-remove` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Run the baseline `collateral-remove` call, then the attacker-shaped one with `amount` relative to the current collateral row (the removing-all branch), and assert the attacker's net token balance change is zero or negative.
