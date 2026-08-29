# Q5768: calc-liq-collateral-repay via liquidate-redeem: make a health check read a different position than the one

## Question
Does `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604) let an unprivileged attacker who controls the seized zToken amount that is immediately redeemed reach `calc-liq-collateral-repay` (mainnet/contracts/market/v0-4-market.clar:728) in a state where it make a health check read a different position than the one that will exist? Given that it scales the repaid debt by `(+ BPS liq-penalty)`, the invariant that conversions never round in the user's favour in either direction breaks and the result is permanent freezing of unclaimed yield.

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:728` -> `calc-liq-collateral-repay`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `calc-liq-collateral-repay` scales the repaid debt by `(+ BPS liq-penalty)`. Reach it through `liquidate-redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: conversions never round in the user's favour in either direction
- Expected Immunefi impact: High - permanent freezing of unclaimed yield
- Fast validation: Write a Clarinet simnet test calling `liquidate-redeem` twice with the seized zToken amount that is immediately redeemed varied, and assert that the value `calc-liq-collateral-repay` returns is identical in both runs; a divergence confirms the finding.
