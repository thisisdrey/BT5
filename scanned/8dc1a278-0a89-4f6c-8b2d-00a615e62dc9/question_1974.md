# Q1974: calc-liq-factor-exp via liquidate-redeem: compose two individually correct mechanisms into an incorr

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the borrower targeted, can an unprivileged attacker make `calc-liq-factor-exp` (mainnet/contracts/market/v0-4-market.clar:708) compose two individually correct mechanisms into an incorrect result? `calc-liq-factor-exp` uses `(/ exp BPS)` as an integer exponent for `pow` and falls back to `sqrti` below BPS, so the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:708` -> `calc-liq-factor-exp`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the borrower targeted
- Exploit idea: `calc-liq-factor-exp` uses `(/ exp BPS)` as an integer exponent for `pow` and falls back to `sqrti` below BPS. Reach it through `liquidate-redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the borrower targeted across its boundary values through `liquidate-redeem` in simnet and assert `calc-liq-factor-exp` never returns a value that breaks the invariant.
