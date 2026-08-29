# Q5977: receive-tokens via collateral-remove-redeem: satisfy a bound with a value the bound was never designed 

## Question
Can an unprivileged attacker entering through `collateral-remove-redeem` (mainnet/contracts/market/v0-4-market.clar:1211), controlling `receiver` for the underlying leg, drive `receive-tokens` (mainnet/contracts/market/v0-market-vault.clar:256) — which pulls an asset from a named account — to satisfy a bound with a value the bound was never designed to admit, breaking the invariant that a value cached within a block still describes the state it was derived from, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/market/v0-market-vault.clar:256` -> `receive-tokens`
- Entrypoint: `collateral-remove-redeem` (`mainnet/contracts/market/v0-4-market.clar:1211`), unprivileged and publicly callable
- Attacker controls: `receiver` for the underlying leg
- Exploit idea: `receive-tokens` pulls an asset from a named account. Reach it through `collateral-remove-redeem` and satisfy a bound with a value the bound was never designed to admit.
- Invariant to test: a value cached within a block still describes the state it was derived from
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `collateral-remove-redeem` with `receiver` for the underlying leg, then read `receive-tokens` state before and after in the same block and assert the two sides of the invariant are equal.
