# Q1733: mask-update via collateral-remove: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `collateral-remove` (mainnet/contracts/market/v0-4-market.clar:1107), controlling the set of assets held, drive `mask-update` (mainnet/contracts/market/v0-market-vault.clar:94) — which sets or clears one bit, clearing only when the row reaches exactly zero — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that no position row exists that the position mask does not represent, and cause protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:94` -> `mask-update`
- Entrypoint: `collateral-remove` (`mainnet/contracts/market/v0-4-market.clar:1107`), unprivileged and publicly callable
- Attacker controls: the set of assets held
- Exploit idea: `mask-update` sets or clears one bit, clearing only when the row reaches exactly zero. Reach it through `collateral-remove` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: no position row exists that the position mask does not represent
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `collateral-remove` call, then the attacker-shaped one with the set of assets held, and assert the attacker's net token balance change is zero or negative.
