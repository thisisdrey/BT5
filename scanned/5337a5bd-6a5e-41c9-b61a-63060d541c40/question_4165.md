# Q4165: resolve-ststx via liquidate: compose two individually correct mechanisms into an incorr

## Question
Can an unprivileged attacker entering through `liquidate` (mainnet/contracts/market/v0-4-market.clar:1382), controlling which collateral and debt asset pair is targeted, drive `resolve-ststx` (mainnet/contracts/market/v0-4-market.clar:339) — which calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down` — to compose two individually correct mechanisms into an incorrect result, breaking the invariant that only the acting principal's own position is mutated, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:339` -> `resolve-ststx`
- Entrypoint: `liquidate` (`mainnet/contracts/market/v0-4-market.clar:1382`), unprivileged and publicly callable
- Attacker controls: which collateral and debt asset pair is targeted
- Exploit idea: `resolve-ststx` calls the external stSTX ratio contract inside price resolution and scales by STSTX-RATIO-DECIMALS with `mul-div-down`. Reach it through `liquidate` and compose two individually correct mechanisms into an incorrect result.
- Invariant to test: only the acting principal's own position is mutated
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `liquidate` with which collateral and debt asset pair is targeted, then read `resolve-ststx` state before and after in the same block and assert the two sides of the invariant are equal.
