# Q1050: is-healthy-with-mask via supply-collateral-add: make a health check read a different position than the one

## Question
Entering through `supply-collateral-add` (mainnet/contracts/market/v0-4-market.clar:1175) while controlling `amount`, can an unprivileged attacker make `is-healthy-with-mask` (mainnet/contracts/market/v0-4-market.clar:663) make a health check read a different position than the one that will exist? `is-healthy-with-mask` resolves an egroup for a caller-influenced mask and applies its LTV-BORROW, so the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV would fail, yielding permanent freezing of a position that can never be closed.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:663` -> `is-healthy-with-mask`
- Entrypoint: `supply-collateral-add` (`mainnet/contracts/market/v0-4-market.clar:1175`), unprivileged and publicly callable
- Attacker controls: `amount`
- Exploit idea: `is-healthy-with-mask` resolves an egroup for a caller-influenced mask and applies its LTV-BORROW. Reach it through `supply-collateral-add` and make a health check read a different position than the one that will exist.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - permanent freezing of a position that can never be closed
- Fast validation: Fuzz `amount` across its boundary values through `supply-collateral-add` in simnet and assert `is-healthy-with-mask` never returns a value that breaks the invariant.
