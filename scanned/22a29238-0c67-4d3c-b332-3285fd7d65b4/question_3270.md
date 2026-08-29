# Q3270: calc-liq-debt-repay via liquidate-redeem: turn an accounting residue into a permanently unclosable p

## Question
Entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) while controlling the seized zToken amount that is immediately redeemed, can an unprivileged attacker make `calc-liq-debt-repay` (mainnet/contracts/market/v0-4-market.clar:723) turn an accounting residue into a permanently unclosable position? `calc-liq-debt-repay` takes the liquidation factor times the debt with `mul-bps-down`, so the invariant that conversions never round in the user's favour in either direction would fail, yielding permanent freezing of funds.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:723` -> `calc-liq-debt-repay`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `calc-liq-debt-repay` takes the liquidation factor times the debt with `mul-bps-down`. Reach it through `liquidate-redeem` and turn an accounting residue into a permanently unclosable position.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: Critical - permanent freezing of funds
- Fast validation: Fuzz the seized zToken amount that is immediately redeemed across its boundary values through `liquidate-redeem` in simnet and assert `calc-liq-debt-repay` never returns a value that breaks the invariant.
