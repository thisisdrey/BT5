# Q0769: resolve-ststx via liquidate-redeem: make two code sites that must agree disagree by an attacke

## Question
Can an unprivileged attacker entering through `liquidate-redeem` (mainnet/contracts/market/v0-4-market.clar:1604), controlling the seized zToken amount that is immediately redeemed, drive `resolve-ststx` (mainnet/contracts/market/v0-4-market.clar:339) — which calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down` — to make two code sites that must agree disagree by an attacker-chosen amount, breaking the invariant that only the acting principal's own position is mutated, and cause direct theft of another user's collateral?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:339` -> `resolve-ststx`
- Entrypoint: `liquidate-redeem` (`mainnet/contracts/market/v0-4-market.clar:1604`), unprivileged and publicly callable
- Attacker controls: the seized zToken amount that is immediately redeemed
- Exploit idea: `resolve-ststx` calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down`. Reach it through `liquidate-redeem` and make two code sites that must agree disagree by an attacker-chosen amount.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of another user's collateral
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate-redeem` with the seized zToken amount that is immediately redeemed, then read `resolve-ststx` state before and after in the same block and assert the two sides of the invariant are equal.
