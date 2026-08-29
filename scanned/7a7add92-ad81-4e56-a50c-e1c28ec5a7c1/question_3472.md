# Q3472: is-healthy-with-mask via liquidate-redeem: reach a state the guard immediately upstream of it never c

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the vault whose share price the redemption moves reach `is-healthy-with-mask` (mainnet/contracts/market/v0-4-market.clar:663) in a state where it reach a state the guard immediately upstream of it never contemplated? Given that it resolves an egroup for a caller-influenced mask and applies its LTV-BORROW, the invariant that only the acting principal's own position is mutated breaks and the result is direct theft of another user's collateral.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:663` -> `is-healthy-with-mask`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `is-healthy-with-mask` resolves an egroup for a caller-influenced mask and applies its LTV-BORROW. Reach it through `liquidate-redeem` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: Set up the position in simnet, call `liquidate-redeem` with the vault whose share price the redemption moves, and assert on the printed event plus the post-state that collateral, debt and share totals still reconcile.
