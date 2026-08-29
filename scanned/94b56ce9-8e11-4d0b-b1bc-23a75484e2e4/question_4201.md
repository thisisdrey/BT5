# Q4201: insert via collateral-remove-redeem: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211), controlling remaining zToken collateral whose price moves with the redeem, drive `insert` (mainnet/contracts/market/v0-market-vault.clar:159) — which rewrites the whole registry entry for a user id — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:159` -> `insert`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: remaining zToken collateral whose price moves with the redeem
- Exploit idea: `insert` rewrites the whole registry entry for a user id. Reach it through `collateral-remove-redeem` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: collateral seized equals debt repaid scaled by the penalty, and only above the liquidation LTV
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-remove-redeem` with remaining zToken collateral whose price moves with the redeem, then read `insert` state before and after in the same block and assert the two sides of the invariant are equal.
