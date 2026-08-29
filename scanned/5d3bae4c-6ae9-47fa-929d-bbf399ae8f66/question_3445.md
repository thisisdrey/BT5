# Q3445: next-index via redeem: make a health check read a different position than the one

## Question
Can an unprivileged attacker entering through `redeem` (mainnet/contracts/vault/v0-vault-stx.clar:797), controlling `recipient`, drive `next-index` (mainnet/contracts/vault/v0-vault-stx.clar:379) — which returns the stale `index` unchanged when the accrue pause state is set, instead of reverting — to make a health check read a different position than the one that will exist, breaking the invariant that every asset a position holds enters the health evaluation exactly once, and cause direct theft of user funds at rest or in motion?

## Target
- File/function: `mainnet/contracts/vault/v0-vault-stx.clar:379` -> `next-index`
- Entrypoint: `redeem` (`mainnet/contracts/vault/v0-vault-stx.clar:797`), unprivileged and publicly callable
- Attacker controls: `recipient`
- Exploit idea: `next-index` returns the stale `index` unchanged when the accrue pause state is set, instead of reverting. Reach it through `redeem` and make a health check read a different position than the one that will exist.
- Invariant to test: every asset a position holds enters the health evaluation exactly once
- Expected Immunefi impact: Critical - direct theft of user funds at rest or in motion
- Fast validation: In `local-testing/tests` on a local fork, drive `redeem` with `recipient`, then read `next-index` state before and after in the same block and assert the two sides of the invariant are equal.
