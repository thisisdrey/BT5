# Q3661: calc-cumulative-debt via accrue: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `accrue` (mainnet/contracts/vault/v0-vault-stx.clar:835), controlling the block time at which accrual is first triggered in a block, drive `calc-cumulative-debt` (mainnet/contracts/vault/v0-vault-stx.clar:180) — which multiplies scaled principal by an index — to make a health check read a different position than the one that will exist, breaking the invariant that every asset a position holds enters the health evaluation exactly once, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:180` -> `calc-cumulative-debt`
- Entrypoint: `accrue` (`mainnet/contracts/vault/v0-vault-stx.clar:835`), unprivileged and publicly callable
- Attacker controls: the block time at which accrual is first triggered in a block
- Exploit idea: `calc-cumulative-debt` multiplies scaled principal by an index. Reach it through `accrue` and make a health check read a different position than the one that will exist.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `accrue` with the block time at which accrual is first triggered in a block, then read `calc-cumulative-debt` state before and after in the same block and assert the two sides of the invariant are equal.
