# Q1514: is-healthy-with-mask via liquidate-redeem: make a health check read a different position than the one

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the vault whose share price the redemption moves, can an unprivileged attacker make `is-healthy-with-mask` (mainnet/contracts/market/v0-4-market.clar:663) make a health check read a different position than the one that will exist? `is-healthy-with-mask` resolves an egroup for a caller-influenced mask and applies its LTV-BORROW, so the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV would fail, yielding protocol insolvency.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:663` -> `is-healthy-with-mask`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the vault whose share price the redemption moves
- Exploit idea: `is-healthy-with-mask` resolves an egroup for a caller-influenced mask and applies its LTV-BORROW. Reach it through `liquidate-redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - protocol insolvency
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the vault whose share price the redemption moves varied, and assert that the value `is-healthy-with-mask` returns is identical in both runs; a divergence confirms the finding.
