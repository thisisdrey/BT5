# Q1249: vault-system-borrow via borrow: reach a state the guard immediately upstream of it never c

## Question
Can an unprivileged attacker entering through `borrow` (mainnet/contracts/market/v0-4-market.clar:1238), controlling `receiver`, including a contract principal, drive `vault-system-borrow` (mainnet/contracts/market/v0-4-market.clar:198) — which routes a borrow to one of six vaults by asset id — to reach a state the guard immediately upstream of it never contemplated, breaking the invariant that a vault's underlying plus outstanding debt covers all shares and all supplier claims, and cause theft of unclaimed yield?

## Target
- File/function: `mainnet/contracts/market/v0-4-market.clar:198` -> `vault-system-borrow`
- Entrypoint: `borrow` (`mainnet/contracts/market/v0-4-market.clar:1238`), unprivileged and publicly callable
- Attacker controls: `receiver`, including a contract principal
- Exploit idea: `vault-system-borrow` routes a borrow to one of six vaults by asset id. Reach it through `borrow` and reach a state the guard immediately upstream of it never contemplated.
- Invariant to test: a vault's underlying plus outstanding debt covers all shares and all supplier claims
- Expected Immunefi impact: High - theft of unclaimed yield
- Fast validation: In `local-testing/tests` on a local fork, drive `borrow` with `receiver`, including a contract principal, then read `vault-system-borrow` state before and after in the same block and assert the two sides of the invariant are equal.
