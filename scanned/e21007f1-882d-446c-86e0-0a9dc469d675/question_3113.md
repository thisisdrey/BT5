# Q3113: get-liquidation-position via liquidate-redeem: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), controlling the borrower targeted, drive `get-liquidation-position` (mainnet/contracts/market/v0-4-market.clar:473) — which returns enabled collateral plus ALL debt, a different view from the one borrow validated against — to make a health check read a different position than the one that will exist, breaking the invariant that a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction, and cause protocol insolvency through uncollateralised debt?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:473` -> `get-liquidation-position`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the borrower targeted
- Exploit idea: `get-liquidation-position` returns enabled collateral plus ALL debt, a different view from the one borrow validated against. Reach it through `liquidate-redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: a resolved price reflects a gated feed whose inputs the caller cannot move in the same transaction
- Expected Immunefi impact: Critical - protocol insolvency through uncollateralised debt
- Fast validation: Run the baseline `liquidate-redeem` call, then the attacker-shaped one with the borrower targeted, and assert the attacker's net token balance change is zero or negative.
